import base64
import glob
import json
import os
import sys

from google.protobuf import descriptor
from google.protobuf.json_format import MessageToJson, ParseDict

from proto import service_pb2

GOLDEN_PATH = "server/mpac_proto/testdata/proto_jsonb_golden.json"
TEST_DB_DIR = "cli/test_db"
MAX_DEPTH = 3
ANY = "google.protobuf.Any"
TIMESTAMP = "google.protobuf.Timestamp"
TIMESTAMP_NANOS = (0, 123000000, 123456000, 123456789)

FD = descriptor.FieldDescriptor
SIGNED_64 = (FD.TYPE_INT64, FD.TYPE_SINT64, FD.TYPE_SFIXED64)
UNSIGNED_64 = (FD.TYPE_UINT64, FD.TYPE_FIXED64)
SIGNED_32 = (FD.TYPE_INT32, FD.TYPE_SINT32, FD.TYPE_SFIXED32)
UNSIGNED_32 = (FD.TYPE_UINT32, FD.TYPE_FIXED32)


def _scalar(field, seed):
    match field.type:
        case FD.TYPE_STRING:
            return f'{field.name}-{seed}-ünïcødé "q"'
        case FD.TYPE_BYTES:
            return bytes((seed * 7 + i) % 256 for i in range(9))
        case FD.TYPE_BOOL:
            return True
        case FD.TYPE_ENUM:
            values = field.enum_type.values
            return values[seed % (len(values) - 1) + 1].number
        case FD.TYPE_FLOAT | FD.TYPE_DOUBLE:
            return 0.1 * (seed + 1)
    if field.type in SIGNED_64:
        return -(2**62) - seed
    if field.type in UNSIGNED_64:
        return 2**63 + seed
    if field.type in SIGNED_32:
        return -1000 - seed
    if field.type in UNSIGNED_32:
        return 4_000_000_000 + seed
    raise ValueError(f"unhandled field type {field.type} for {field.full_name}")


def _populate(message, seed, depth):
    if message.DESCRIPTOR.full_name == TIMESTAMP:
        message.seconds = 1_761_900_000 + seed
        message.nanos = TIMESTAMP_NANOS[seed % len(TIMESTAMP_NANOS)]
        return
    if message.DESCRIPTOR.full_name == ANY:
        packed = service_pb2.Visibility()
        _populate(packed, seed, depth + 1)
        message.Pack(packed)
        return
    if depth > MAX_DEPTH:
        return
    for index, field in enumerate(message.DESCRIPTOR.fields):
        field_seed = seed + index + 1
        container = getattr(message, field.name)
        is_message = field.type == FD.TYPE_MESSAGE
        if field.message_type is not None and field.message_type.GetOptions().map_entry:
            key_field = field.message_type.fields_by_name["key"]
            value_field = field.message_type.fields_by_name["value"]
            for offset in range(2):
                key = _scalar(key_field, field_seed + offset)
                if value_field.type == FD.TYPE_MESSAGE:
                    _populate(container[key], field_seed + offset, depth + 1)
                else:
                    container[key] = _scalar(value_field, field_seed + offset)
        elif field.label == FD.LABEL_REPEATED:
            for offset in range(2):
                if is_message:
                    _populate(container.add(), field_seed + offset, depth + 1)
                else:
                    container.append(_scalar(field, field_seed + offset))
        elif is_message:
            _populate(container, field_seed, depth + 1)
        else:
            setattr(message, field.name, _scalar(field, field_seed))


def _entry(message, variant):
    return {
        "message": message.DESCRIPTOR.full_name,
        "variant": variant,
        "proto_bytes": base64.b64encode(
            message.SerializeToString(deterministic=True)
        ).decode("ascii"),
        "proto_json": json.loads(
            MessageToJson(
                message=message,
                always_print_fields_with_no_presence=True,
                preserving_proto_field_name=True,
                indent=0,
            )
        ),
    }


def _seed_entries():
    entries = []
    for path in sorted(glob.glob(os.path.join(TEST_DB_DIR, "*.json"))):
        with open(path, encoding="utf-8") as f:
            test = ParseDict(json.load(f), service_pb2.Test())
        entries.append(_entry(test, f"seed:{os.path.basename(path)}"))
    for path in sorted(glob.glob(os.path.join(TEST_DB_DIR, "items", "*.json"))):
        with open(path, encoding="utf-8") as f:
            items = json.load(f)
        for index, item in enumerate(items):
            test_item = ParseDict(
                item, service_pb2.TestItem(), ignore_unknown_fields=True
            )
            test_item.id = f"{test_item.test_id}-{index}"
            entries.append(
                _entry(test_item, f"seed:items/{os.path.basename(path)}:{index}")
            )
    return entries


def generate():
    entries = _seed_entries()
    for name in sorted(service_pb2.DESCRIPTOR.message_types_by_name):
        message_class = getattr(service_pb2, name)
        entries.append(_entry(message_class(), "empty"))
        populated = message_class()
        _populate(populated, seed=0, depth=0)
        entries.append(_entry(populated, "populated"))
    return entries


def render(entries):
    return json.dumps(entries, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def main():
    workspace = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if not workspace:
        sys.stdout.write(render(generate()))
        return
    os.chdir(workspace)
    with open(os.path.join(workspace, GOLDEN_PATH), "w", encoding="utf-8") as f:
        f.write(render(generate()))


if __name__ == "__main__":
    main()
