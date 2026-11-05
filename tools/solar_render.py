#!/usr/bin/env python
"""Render a structure video with SolarRenderer through WanGP's Python API. Run with WanGP's own Python.

  python tools/solar_render.py --wangp <WanGP app> --structure s.mp4 --first f.png --prompt p.txt --out out.mp4
  python tools/solar_render.py ... --fasth3 --phases 2 --resolution 864x480
"""
import argparse
import os
import shutil
import sys
import time
from pathlib import Path


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wangp", required=True, help="the WanGP app folder")
    ap.add_argument("--structure", required=True)
    ap.add_argument("--first", default="", help="start image, made from frame 0 of the structure")
    ap.add_argument("--prompt", required=True, help="text file with the six-part prompt")
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--frames", type=int, default=158, help="17n + 5; trained at 158")
    ap.add_argument("--resolution", default="1344x768")
    ap.add_argument("--fasth3", action="store_true", help="FastH3 Omni instead of MiniMax H3 Ref2VA Pruned")
    ap.add_argument("--phases", type=int, default=1, choices=(1, 2), help="2 needs --fasth3")
    a = ap.parse_args()
    if a.phases == 2 and not a.fasth3:
        ap.error("--phases 2 needs --fasth3: regular H3 runs SolarRenderer in one phase")
    root = Path(a.wangp).resolve()
    structure, first, out = Path(a.structure).resolve(), Path(a.first).resolve() if a.first else None, Path(a.out).resolve()
    prompt = Path(a.prompt).read_text(encoding="utf-8")
    os.chdir(root)
    sys.path.insert(0, str(root))
    from shared.api import init

    session = init(root=root, console_output=True)
    settings = {
        "model_type": "minimax_h3_fasth3_omni" if a.fasth3 else "minimax_h3_ref2va_pruned", "prompt": prompt, "multi_prompts_gen_type": "FG",
        "resolution": a.resolution, "video_length": a.frames, "force_fps": 24, "seed": a.seed,
        "num_inference_steps": 8 if a.fasth3 else 4, "num_inference_steps2": 3, "sample_solver": "euler", "flow_shift": 12.0,
        "guidance_phases": a.phases, "guidance_scale": 1.0, "custom_sigmas": "", "skip_steps_cache_type": "", "sliding_window_size": 362,
        "video_prompt_type": "V", "video_guide": str(structure), "audio_prompt_type": "",
        # The 4-step LoRA in Two Phases lays a visible mesh over the frame: Two Phases runs the control LoRA alone.
        "activated_loras": ["solarrenderer_control_lora.safetensors"] if a.phases == 2 else ["solarrenderer_4step_lora.safetensors", "solarrenderer_control_lora.safetensors"],
        "loras_multipliers": "1;1" if a.phases == 2 else "1.0 1.0", "custom_settings": {"audio_refinement": "none"},
    }
    if first:
        settings.update(image_prompt_type="S", image_start=str(first))
    began = time.time()
    job = session.submit_task(settings)
    last = None
    for event in job.events.iter(timeout=0.5):
        if event.kind == "progress":
            p = event.data
            key = (p.status, p.current_step)
            if key != last:
                last = key
                print(f"[progress] {p.progress:3d}% {p.status or p.phase} {p.current_step}/{p.total_steps}", flush=True)
    result = job.result()
    if not result.success or not result.generated_files:
        sys.exit("SolarRenderer render failed: " + "; ".join(str(getattr(e, "message", e)) for e in result.errors))
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(result.generated_files[-1], out)
    print(f"DONE {out} in {time.time() - began:.0f} s", flush=True)


if __name__ == "__main__":
    main()
