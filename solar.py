"""SolarRenderer's sampling contract on WanGP's H3 pipeline, for one generate() at a time.

Upstream (scripts/render.py, comfyui/nodes.py) needs four things beyond its two LoRAs. WanGP has each of them behind a switch:
1. Structure video and first frame laid over the target as frame-aligned keyframes: pipeline.H3_ALIGNED_CONTROL_VIDEO.
2. The prompt encoder sees both as <Video 1> and <Video 2> at 2 fps: the aligned path hides them, so _encode_prompt is wrapped.
3. Four evaluations with fresh noise between them, shift 12 / audio 3: the Pika re-noise solver with another schedule.
4. The soundtrack held at encoded silence: a transformer forward pre-hook.
"""
import functools
import inspect
import os
import sys
import time

import numpy
import torch
from safetensors.torch import load_file

SETTING_ID = "solar_renderer"
STEPS = 4
SHIFT_VIDEO, SHIFT_AUDIO = 12.0, 3.0
FPS = 24
SILENCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "h3_silence_153_158_170.safetensors")
_GENERATE_PARAMS = ("loras_selected", "input_frames", "image_start", "video_prompt_type", "sample_solver", "sampling_steps", "shift", "seed",
                    "height", "width", "custom_settings", "refinement_mode")
_MODULE_NAMES = ("H3_ALIGNED_CONTROL_VIDEO", "PIKA_SOLVER", "PIKA_STEPS", "pika_sigma_schedule", "_as_video", "_qwen_frames", "_resize_video")
_FORWARD_PARAMS = ["self", "video_x", "audio_x", "sigma_video", "sigma_audio", "context", "payload"]


def sigma_schedule(device):
    base = torch.linspace(1.0, 0.0, STEPS + 1, dtype=torch.float64, device="cpu")
    return tuple((shift * base / (1.0 + (shift - 1.0) * base)).to(device=device, dtype=torch.float32) for shift in (SHIFT_VIDEO, SHIFT_AUDIO))


def pil_to_video(image):
    """[C, 1, H, W] in -1..1, the layout the pipeline's frames use."""
    array = torch.from_numpy(numpy.asarray(image.convert("RGB")).copy()).float().div_(127.5).sub_(1.0)
    return array.permute(2, 0, 1).unsqueeze(1)


def shown_items(module, input_frames, image_start, height, width):
    """Upstream's presentation: every 12th structure frame, then the first frame held for 13 frames (= 2 samples at 2 fps)."""
    video = module._as_video(input_frames)
    if tuple(video.shape[-2:]) != (height, width):
        video = module._resize_video(video, height, width)
    indices = list(range(0, video.shape[1], FPS // 2))
    items = [{"type": "video", "frames": module._qwen_frames(video[:, indices].clone()), "timestamps": [i / 2.0 for i in range(len(indices))]}]
    if image_start is not None:
        if torch.is_tensor(image_start):  # WanGP's generation loop hands the start image over as a tensor
            still = module._as_video(image_start)[:, :1]
            if tuple(still.shape[-2:]) != (height, width):
                still = module._resize_video(still, height, width)
        else:
            still = pil_to_video(image_start if image_start.size == (width, height) else image_start.resize((width, height)))
        items.append({"type": "video", "frames": module._qwen_frames(still.repeat(1, 2, 1, 1)), "timestamps": [0.0, 0.5]})
    return items


class SilenceHook:
    """Upstream never samples a soundtrack: it noises H3's encoded silence to the step's audio sigma with one fixed draw."""

    def __init__(self, seed):
        self.silence = load_file(SILENCE)["silence_170f"].float().permute(1, 0, 2).unsqueeze(0)  # [1, 32, 2, T], WanGP's audio layout
        self.generator = torch.Generator("cpu").manual_seed(int(seed))
        self.noise = None

    def __call__(self, _module, args, kwargs):
        audio, sigma = args[1], float(args[3].flatten()[0])
        length = audio.shape[-1]
        clean = self.silence
        if clean.shape[-1] < length:  # the encoded silence is steady away from its ends (upstream extend_silence)
            middle = clean[..., clean.shape[-1] // 2:clean.shape[-1] // 2 + 1]
            clean = torch.cat((clean, middle.repeat(1, 1, 1, length - clean.shape[-1])), dim=-1)
        clean = clean[..., :length]
        if self.noise is None or self.noise.shape != clean.shape:
            self.noise = torch.randn(clean.shape, generator=self.generator, dtype=torch.float32)
        held = ((1.0 - sigma) * clean + sigma * self.noise).to(device=audio.device, dtype=audio.dtype)
        return (args[0], held, *args[2:]), kwargs


def check_compat(pipeline_cls):
    problems = []
    params = inspect.signature(pipeline_cls.generate).parameters
    problems += [f"generate() lost '{name}'" for name in _GENERATE_PARAMS if name not in params]
    module = sys.modules[pipeline_cls.__module__]
    problems += [f"pipeline.{name} missing" for name in _MODULE_NAMES if not hasattr(module, name)]
    from models.minimax_h3.transformer import MiniMaxH3Model
    forward = list(inspect.signature(MiniMaxH3Model.forward).parameters)[:7]
    if forward != _FORWARD_PARAMS:
        problems.append(f"MiniMaxH3Model.forward{tuple(forward)} != {tuple(_FORWARD_PARAMS)}")
    return problems


def install(pipeline_cls):
    if getattr(pipeline_cls, "_h3_solar_installed", False):
        return []
    problems = check_compat(pipeline_cls)
    if problems:
        return problems
    original = pipeline_cls.generate
    signature = inspect.signature(original)

    @functools.wraps(original)
    def generate(self, *args, **kwargs):
        bound = signature.bind(self, *args, **kwargs)
        settings = bound.arguments.get("custom_settings") or {}
        # selecting the SolarRenderer LoRAs IS the switch. WanGP silently drops custom settings past its 5-slot cap, and on stock
        # Ref2VA Pruned that dropped this one, and renders ran the LoRAs alone while looking fine. FastH3 keeps its own fixed solver.
        loras = str(bound.arguments.get("loras_selected") or "")
        requested = str(settings.get(SETTING_ID) or "").strip().lower() if isinstance(settings, dict) else ""
        mode = "" if "solarrenderer_" not in loras and not requested else "aligned" if getattr(self.transformer, "fasth3_inference", None) is not None else "on"
        if not mode or bound.arguments.get("refinement_mode"):
            return original(self, *args, **kwargs)
        bound.apply_defaults()
        arguments = bound.arguments
        prompt_type = arguments.get("video_prompt_type") or ""
        if not self.reference_mode or self.fixed_prompt is not None or "V" not in prompt_type or "G" in prompt_type or "-" in prompt_type:
            raise ValueError("SolarRenderer needs H3 Ref2VA with video_prompt_type V and a structure video as video_guide")
        if arguments.get("input_frames") is None:
            raise ValueError("SolarRenderer needs a structure video (video_guide)")
        module = sys.modules[type(self).__module__]
        # "aligned" keeps the model's own solver and steps, for students with a fixed contract such as FastH3 Omni (8 steps, Euler).
        if mode == "on":
            arguments.update(sample_solver=module.PIKA_SOLVER, sampling_steps=STEPS, shift=SHIFT_VIDEO)
        items = shown_items(module, arguments["input_frames"], arguments.get("image_start"), arguments["height"], arguments["width"])
        encode_prompt = self._encode_prompt

        def shown_encode(prompt, presentation):
            return encode_prompt(prompt, [*items, *presentation])

        # these module globals are read inside generate(); restored in finally, so plain H3 and real Pika jobs keep their contract.
        saved = module.H3_ALIGNED_CONTROL_VIDEO, module.PIKA_STEPS, module.pika_sigma_schedule
        module.H3_ALIGNED_CONTROL_VIDEO, module.PIKA_STEPS, module.pika_sigma_schedule = True, STEPS, sigma_schedule
        self._encode_prompt = shown_encode
        hook = self.transformer.register_forward_pre_hook(SilenceHook(arguments.get("seed") or 0), with_kwargs=True)
        # WanGP's API session swallows prints, so each job leaves a line on disk: the proof this contract actually ran.
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "solar_jobs.log"), "a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%H:%M:%S')} mode={mode} solver={arguments.get('sample_solver')} steps={arguments.get('sampling_steps')} "
                      f"aligned={module.H3_ALIGNED_CONTROL_VIDEO} first_frame={type(arguments.get('image_start')).__name__} shown={len(items)}\n")
        print(f"[H3 SolarRenderer] mode {mode}, {arguments.get('sampling_steps')} steps, aligned structure video, first frame: {arguments.get('image_start') is not None}")
        try:
            return original(*bound.args, **bound.kwargs)
        finally:
            hook.remove()
            del self._encode_prompt
            module.H3_ALIGNED_CONTROL_VIDEO, module.PIKA_STEPS, module.pika_sigma_schedule = saved

    pipeline_cls.generate = generate
    pipeline_cls._h3_solar_installed = True
    return []
