"""Resolvera parameterized templates till konkreta pass.

Templates har två typer av flexibilitet:
1. `duration_pct: 0.70` — andel av totalduration. Multipliceras med total.
2. `duration_min: "{duration_min} - 15"` — uttrycksstring med parametrar.

Resolvern tar in ett template-pass + parameter-värden och returnerar
ett pass där alla uttryck är ersatta med konkreta värden.

Av säkerhetsskäl används inte eval() utan en restriktiv uttrycksparser
som bara accepterar tal, parameter-referenser, och +/-/*//-operatorer.
"""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any


# Tillåtna tokens i ett aritmetiskt uttryck efter parameter-substitution:
# bara siffror, whitespace, +/-/*//, punkt (decimaltal), paranteser.
# Om resultatet innehåller bokstäver eller andra textuella tecken,
# tolkar vi det som textsubstitution, inte ett uttryck.
_ARITHMETIC_PATTERN = re.compile(r"^[\d\s+\-*/().]+$")
_PARAM_REF = re.compile(r"\{(\w+)\}")


class TemplateError(ValueError):
    """Fel vid template-resolution."""


def _evaluate_expr(expr: str, params: dict[str, Any]) -> Any:
    """Evaluera ett uttryck som '{duration_min} - 15' mot params.

    Begränsad uttrycksparser — bara aritmetik och parameter-refs.
    """
    if not isinstance(expr, str):
        return expr  # tal eller annat, returnera oförändrat

    # Om strängen inte innehåller någon parameter-ref och inte är ett uttryck,
    # returnera den som den är (vanlig text)
    if "{" not in expr:
        # Inte ett uttryck — ren textsträng
        return expr

    # Ersätt parameter-refs med konkreta värden
    def replace_ref(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in params:
            raise TemplateError(f"Okänd parameter: {{{name}}}")
        return str(params[name])

    resolved = _PARAM_REF.sub(replace_ref, expr)

    # Om resultatet bara består av matte-tecken (siffror, +/-/*///,
    # paranteser, decimal-punkt, whitespace) — försök evaluera som uttryck.
    # Annars: returnera som ren textsträng (parameter har bäddats in i prosa,
    # t.ex. "Bygg upp till {target_cadence} rpm").
    if not _ARITHMETIC_PATTERN.match(resolved.strip()):
        return resolved

    # Försök som enkelt tal först
    try:
        return int(resolved)
    except ValueError:
        pass
    try:
        return float(resolved)
    except ValueError:
        pass

    # Annars: evaluera som aritmetiskt uttryck med begränsat scope
    try:
        result = eval(resolved, {"__builtins__": {}}, {})  # noqa: S307
    except Exception as exc:
        raise TemplateError(f"Kunde inte evaluera {expr!r}: {exc}") from exc

    if isinstance(result, float) and result.is_integer():
        return int(result)
    return result


def _resolve_value(value: Any, params: dict[str, Any], total_min: float | None) -> Any:
    """Rekursivt resolva ett värde — sträng-uttryck, lista, dict."""
    if isinstance(value, str):
        return _evaluate_expr(value, params)
    if isinstance(value, list):
        return [_resolve_value(v, params, total_min) for v in value]
    if isinstance(value, dict):
        return {k: _resolve_value(v, params, total_min) for k, v in value.items()}
    return value


def _resolve_segment_duration(seg: dict, total_min: float) -> dict:
    """Konvertera duration_pct till duration_min på ett segment."""
    if "duration_pct" in seg and "duration_min" not in seg:
        pct = seg["duration_pct"]
        seg["duration_min"] = round(total_min * pct)
        # Behåll duration_pct för spårbarhet men markera som resolved
    return seg


def resolve_template(
    workout: dict[str, Any],
    parameter_values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolva en parameterized template till ett konkret pass.

    Om passet inte är parameterized returneras det oförändrat.

    Args:
        workout: pass-dict från YAML
        parameter_values: konkreta värden för parametrar, t.ex.
            {"duration_min": 90}. Saknade parametrar fyller default.

    Returns:
        Pass-dict där alla {param}-uttryck är ersatta med värden och
        alla duration_pct är konverterade till duration_min.
    """
    if not workout.get("parameterized"):
        return workout

    parameter_values = parameter_values or {}

    # Hämta parameter-definitioner och fyll defaults
    param_defs = workout.get("parameters", {})
    params: dict[str, Any] = {}
    for name, spec in param_defs.items():
        if name in parameter_values:
            params[name] = parameter_values[name]
        elif isinstance(spec, dict) and "default" in spec:
            params[name] = spec["default"]
        else:
            raise TemplateError(
                f"Parameter {name!r} saknar både värde och default i {workout.get('code')}"
            )

    # Total duration används för duration_pct-konvertering
    total_min: float | None = params.get("duration_min")

    # Kopiera passet djupt så vi inte modifierar originalet
    resolved = deepcopy(workout)

    # Resolva alla string-uttryck rekursivt
    resolved = _resolve_value(resolved, params, total_min)

    # Konvertera duration_pct → duration_min på segmenten
    if total_min is not None:
        for seg in resolved.get("main_set", []):
            _resolve_segment_duration(seg, total_min)

    # Markera som resolved och behåll parameter-värden för spårbarhet
    resolved["_resolved_parameters"] = params

    return resolved
