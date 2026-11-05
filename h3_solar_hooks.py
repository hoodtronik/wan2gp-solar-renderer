"""Early hook for the SolarRenderer plugin, registered as a WanGP "model handler" with no model types.

Model handlers are imported in every mode (GUI, --deepy-server,
in-process API), plugin.py only in GUI mode. Importing this module installs the pipeline wrap everywhere.
"""
try:
    from . import solar
except Exception as error:  # a broken plugin file must never stop WanGP from starting
    solar = None
    print(f"[H3 SolarRenderer] failed to import: {error!r}")

REF_ARCHITECTURES = ("minimax_h3_ref2va", "minimax_h3_ref2va_pruned")
SETTING = {
    "id": "solar_renderer",
    "name": "SolarRenderer",
    "label": "SolarRenderer: selecting its LoRAs turns it on by itself; type on to force it. Lays the V control video over the target frame for frame (six-part prompt)",
    "type": "text",
    "default": "",
}
DEEPY_NOTE = (" SolarRenderer plugin: to render a structure video (grey box, depth, skeletons, segmentation) frame for frame, select the LoRAs "
              "solarrenderer_4step_lora and solarrenderer_control_lora (selecting them turns the mode on), video_prompt_type V, the structure as video_guide, "
              "the look as image_start (image_prompt_type S), 158 frames at 1344x768. The prompt names <Video 1> = structure and <Video 2> = first frame. "
              "It generates no usable sound; add audio afterwards.")


def _with_setting(model_def):
    if not isinstance(model_def, dict):
        return model_def
    settings = list(model_def.get("custom_settings") or [])
    if not any(isinstance(s, dict) and s.get("id") == solar.SETTING_ID for s in settings):
        model_def["custom_settings"] = settings + [dict(SETTING)]
    if isinstance(model_def.get("deepy_infos"), str) and solar.SETTING_ID not in model_def["deepy_infos"]:
        model_def["deepy_infos"] += DEEPY_NOTE
    return model_def


def activate():
    from models.minimax_h3 import minimax_h3_handler as handler
    from models.minimax_h3.pipeline import MiniMaxH3Pipeline

    problems = solar.install(MiniMaxH3Pipeline)
    if problems:
        print("[H3 SolarRenderer] DISABLED, WanGP's H3 pipeline changed: " + "; ".join(problems))
        return
    family = handler.family_handler
    original = family.query_model_def
    if not getattr(original, "_h3_solar", False):
        def query_model_def(base_model_type, model_def):
            result = original(base_model_type, model_def)
            if base_model_type not in REF_ARCHITECTURES:
                return result
            # wgp.py forces FastH3 Omni to one phase through these flags, but pipeline.py has PDD phase-2 code. This offers the option only; FastH3 still defaults to one phase.
            if isinstance(result, dict) and isinstance(model_def, dict) and model_def.get("fasth3"):
                result.update({"guidance_max_phases": 2, "lock_guidance_phases": False, "visible_phases": 2, "inference_steps2": True, "lora_multiplier_phases": 2})
            return _with_setting(result)

        query_model_def._h3_solar = True
        family.query_model_def = staticmethod(query_model_def)
    print("[H3 SolarRenderer] active")


class family_handler:
    """WanGP model-handler contract with zero model types; it exists only so this module gets imported early."""

    @staticmethod
    def query_supported_types():
        return []

    @staticmethod
    def query_family_maps():
        return {}, {}

    @staticmethod
    def query_family_infos():
        return {}


try:
    if solar is not None:
        activate()
except Exception as error:  # never break WanGP startup
    print(f"[H3 SolarRenderer] failed to activate: {error!r}")
