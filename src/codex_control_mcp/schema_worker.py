"""Private, bounded-input schema computation worker; never invokes tools."""
import json
import sys

from jsonschema import Draft7Validator
from jsonschema.validators import validator_for
from referencing import Registry


def main():
    try:
        raw = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("input budget")
        schema, arguments = json.loads(raw)
        validator_class = validator_for(schema, default=Draft7Validator)
        error = next(validator_class(schema, registry=Registry()).iter_errors(arguments), None)
        verdict = {"valid": error is None, "rule": error.validator if error is not None else None}
        print(json.dumps(verdict))
        return 0
    except Exception:
        print(json.dumps({"valid": False, "rule": "schema_invalid"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
