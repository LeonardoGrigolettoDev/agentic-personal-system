"""Validate every infra/railway/*/railway.json against Railway's published schema (vendored copy) - stdlib only.

Supports the JSON-Schema subset that schema uses (type/const/enum/anyOf/properties/additionalProperties/items/
minimum/maximum/maxItems/propertyNames) and is stricter in one way: unknown keys under build/deploy are errors,
because Railway silently ignores them. Refresh the copy with:
    curl -fsSL https://railway.com/railway.schema.json -o infra/railway/tests/railway.schema.json
"""

import json
import sys
from pathlib import Path

TYPES = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}


def errors(value, schema: dict, path: str) -> list[str]:
    if "anyOf" in schema:
        branches = [errors(value, s, path) for s in schema["anyOf"]]
        return [] if any(not b for b in branches) else [f"{path}: matches none of anyOf ({branches[0][0]})"]
    out: list[str] = []
    t = schema.get("type")
    if t and not TYPES[t](value):
        return [f"{path}: expected {t}, got {type(value).__name__}"]
    if "const" in schema and value != schema["const"]:
        out.append(f"{path}: expected {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{path}: {value} < {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            out.append(f"{path}: {value} > {schema['maximum']}")
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            out.append(f"{path}: more than {schema['maxItems']} items")
        for i, item in enumerate(value):
            out += errors(item, schema.get("items", {}), f"{path}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for k, v in value.items():
            if k in props:
                out += errors(v, props[k], f"{path}.{k}")
            elif isinstance(schema.get("additionalProperties"), dict):
                out += errors(v, schema["additionalProperties"], f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                out.append(f"{path}: unknown key {k!r}")
    return out


def main(root: Path) -> int:
    schema = json.loads((root / "tests/railway.schema.json").read_text(encoding="utf-8"))
    files = sorted(root.glob("*/railway.json"))
    if not files:
        print("no railway.json files found", file=sys.stderr)
        return 1
    failed = 0
    for f in files:
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            print(f"FAIL {f.relative_to(root)}: invalid JSON: {exc}")
            failed += 1
            continue
        errs = errors(doc, schema, "$")
        for section in ("build", "deploy"):
            known = schema["properties"][section]["properties"]
            errs += [f"$.{section}: unknown key {k!r}" for k in doc.get(section, {}) if k not in known]
        errs += [f"$: unknown top-level key {k!r}" for k in doc if k not in schema["properties"]]
        if errs:
            failed += 1
            print(f"FAIL {f.relative_to(root)}")
            for e in errs:
                print(f"     {e}")
        else:
            print(f"ok   {f.relative_to(root)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]))
