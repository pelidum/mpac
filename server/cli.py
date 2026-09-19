import concurrent
import gc
import gzip
import hashlib
import json
import math
import os
import time
import traceback

from typing import List

from absl import app, flags, logging

import filetype
import grpc
import orjson
import pandas as pd

from server import server as mpac_server
from server import service_pb2
from server import service_pb2_grpc

from google.auth import default
from google.auth.transport.requests import Request
from google.protobuf.empty_pb2 import Empty
from google.protobuf import timestamp_pb2
from google.protobuf.json_format import MessageToDict, MessageToJson, Parse, ParseDict
from tqdm import tqdm


VALID_MPAC_OBJECTS = [x for x in mpac_server.MPAC_OBJECTS.keys()]
VALID_MPAC_OBJECTS.append("models")


def retry_on_resource_exhausted(func, max_retries=5, initial_backoff=0.1):
    def wrapper(*args, **kwargs):
        backoff = initial_backoff
        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except grpc.RpcError as e:
                if e.code() == grpc.StatusCode.RESOURCE_EXHAUSTED:
                    if attempt < max_retries - 1:
                        time.sleep(backoff)
                        backoff *= 2
                        continue
                raise
        return None

    return wrapper


FLAGS = flags.FLAGS
flags.DEFINE_boolean(
    "bootstrap",
    False,
    "Bootstraps the eval and survey DBs at startup. Useful for first-time MPAC server init.",
)
flags.DEFINE_multi_string(
    "bootstrap_include",
    None,
    "Only include specific test IDs for bootstrapping.",
)
flags.DEFINE_string("mpac_server_host", "localhost", "Host address of the server")
flags.DEFINE_integer("mpac_server_port", 50051, "Port number of the server")
flags.DEFINE_string(
    "mpac_api_key",
    None,
    "The API key to use for authentication.",
)
flags.DEFINE_string(
    "mpac_cert_file",
    None,
    "Path to a local cert file, used in local dev. Should not be needed in prod.",
)
flags.DEFINE_multi_string(
    "labels",
    [],
    "Labels to use in MPAC survey runs.",
)
flags.DEFINE_bool(
    "get_credits",
    False,
    "Whether or not to print the remaining credits available. Only compatible with OpenRouter API.",
)
flags.DEFINE_multi_string(
    "models",
    None,
    "Models to use in MPAC survey runs.",
)
flags.DEFINE_enum(
    "object_type",
    None,
    VALID_MPAC_OBJECTS,
    "The object type to perform actions against.",
)
flags.DEFINE_bool(
    "output_json",
    False,
    "Whether or not to output objects as JSON instead of Protobuf.",
)
flags.DEFINE_bool(
    "secure_channel",
    False,
    "Whether or not to use a secure gRPC channel (required for prod envs.)",
)
flags.DEFINE_string(
    "id",
    None,
    "The id to use for a given object type.",
)
flags.DEFINE_bool(
    "list",
    False,
    "Perform a list action against object_type.",
)
flags.DEFINE_string(
    "create",
    None,
    "Perform a create action against object_type, string argument will be parsed as a valid JSON object string.",
)
flags.DEFINE_string(
    "update",
    None,
    "Perform a create action against object_type, string argument will be parsed as a valid JSON object string with 'id' populated and corresponding to a pre-existing report ID.",
)
flags.DEFINE_bool(
    "delete",
    False,
    "Perform a delete action against object_type, combine with --id",
)


def generic_list_obj(objects, columns: List = []):
    obj_dicts = []
    for object in objects:
        obj_dicts.append(
            MessageToDict(
                message=object,
                always_print_fields_with_no_presence=True,
                preserving_proto_field_name=True,
            )
        )

    obj_df = pd.DataFrame(obj_dicts)
    if len(columns) > 0:
        obj_df = obj_df[columns]

    print(obj_df)

    obj_list = [x.get("id") for x in obj_dicts]
    print(obj_list)


def bootstrap_tests(mpac_api_key_metadata, mpac_service):
    logging.info("Bootstrapping tests...")
    current_dir = os.path.dirname(os.path.abspath(__file__))
    test_dir = os.path.join(current_dir, "test_db")
    test_item_dir = os.path.join(test_dir, "items")
    test_attachment_dir = os.path.join(test_dir, "attachments")
    created_test_ids = bootstrap_test_definitions(
        test_dir, mpac_api_key_metadata, mpac_service
    )

    if created_test_ids:
        logging.info(f"Successfully created {len(created_test_ids.keys())} tests")
        logging.info("Verifying all tests are committed...")

        verified_tests = set()
        for test_name, test_id in created_test_ids.items():
            try:
                mpac_service.GetTest(
                    request=service_pb2.GetRequest(id=test_id),
                    metadata=mpac_api_key_metadata,
                )
                verified_tests.add(test_id)
            except Exception as e:
                logging.error(f"Test {test_id} verification failed: {e}")

        logging.info(
            f"Verified {len(verified_tests)}/{len(created_test_ids.keys())} tests"
        )

        if verified_tests:
            bootstrap_test_items(
                test_item_dir,
                test_attachment_dir,
                created_test_ids,
                mpac_api_key_metadata,
                mpac_service,
            )
        else:
            logging.error("No tests verified, skipping item creation")
    else:
        logging.warning("No tests created, skipping item creation")


def bootstrap_test_definitions(test_dir, mpac_api_key_metadata, mpac_service):
    default_acls = service_pb2.Visibility()
    default_acls.type = service_pb2.Visibility.VisibilityOptions.VISIBILITY_ORG_ONLY
    test_def_files = [f for f in os.listdir(test_dir) if f.endswith(".json")]
    created_test_ids = {}
    for test_def_file in test_def_files:
        try:
            test_path = os.path.join(test_dir, test_def_file)
            with open(test_path) as f:
                test_json = json.load(f)

            test_pb = service_pb2.Test()
            test_pb.created_at_utc.GetCurrentTime()
            test_pb.modified_at_utc.GetCurrentTime()
            ParseDict(js_dict=test_json, message=test_pb)
            test_pb.visibility.CopyFrom(default_acls)
            if should_bootstrap(test_pb.id):
                response = mpac_service.CreateTest(
                    request=test_pb,
                    metadata=mpac_api_key_metadata,
                )
                if response.code == service_pb2.ResponseCode.SUCCESS:
                    created_test_ids[test_pb.id] = response.id
                    logging.info(f"Created test: {test_pb.id} as {response.id}")
                else:
                    logging.error(
                        f"Failed to create test {test_pb.id}: {response.reason}"
                    )

        except Exception as e:
            logging.error(f"Failed to import test {test_def_file}: {e}")

    return created_test_ids


def bootstrap_test_items(
    test_item_dir,
    test_attachment_dir,
    created_test_ids,
    mpac_api_key_metadata,
    mpac_service,
):
    target_files = [
        f for f in os.listdir(test_item_dir) if f.endswith((".json", ".json.gz"))
    ]
    if FLAGS.bootstrap_include:
        target_files = [
            f for f in target_files if f.split(".")[0] in FLAGS.bootstrap_include
        ]

    for filename in target_files:
        try:
            file_path = os.path.join(test_item_dir, filename)
            test_items = load_json_data(file_path)

            if not test_items:
                logging.warning(f"No items loaded from {filename}, skipping...")
                continue

            test_id = test_items[0].get("test_id") if test_items else None
            if not test_id:
                logging.error(f"No test_id found in {filename}, skipping...")
                continue

            if test_id not in created_test_ids.keys():
                logging.warning(
                    f"Test {test_id} not in verified tests, skipping {filename}"
                )
                continue

            logging.info(
                f"Processing {len(test_items)} items from {filename} for test {test_id}"
            )

            attachment_futures = []
            attachment_map = {}

            with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                for item in test_items:
                    if att_relpath := item.get("attachment_path"):
                        att_fqn = os.path.join(
                            test_attachment_dir,
                            item.get("test_id"),
                            att_relpath,
                        )
                        if os.path.isfile(att_fqn):
                            future = executor.submit(
                                create_attachment,
                                att_relpath,
                                att_fqn,
                                mpac_api_key_metadata,
                                mpac_service,
                            )
                            attachment_futures.append(
                                (future, item.get("test_id"), att_relpath)
                            )

                for future, test_id, att_relpath in tqdm(
                    attachment_futures,
                    desc=f"Creating attachments for {filename}",
                ):
                    try:
                        attachment_id = future.result()
                        if attachment_id:
                            attachment_map[f"{test_id}/{att_relpath}"] = attachment_id
                    except Exception as e:
                        logging.error(
                            f"Attachment creation failed for {att_relpath}: {e}"
                        )

            batch_size = 1000
            total_items = len(test_items)

            for batch_start in range(0, total_items, batch_size):
                batch_end = min(batch_start + batch_size, total_items)
                batch = test_items[batch_start:batch_end]

                batch_request = service_pb2.BatchCreateTestItemsRequest()
                for item in batch:
                    test_id = item.get("test_id")
                    test_uid = created_test_ids[test_id]
                    test_item_hash = hashlib.sha256(orjson.dumps(item)).hexdigest()
                    test_item_pb = service_pb2.TestItem()
                    ParseDict(item, test_item_pb, ignore_unknown_fields=True)
                    test_item_pb.id = test_item_hash
                    test_item_pb.test_id = test_uid
                    if att_relpath := item.get("attachment_path"):
                        test_id = item.get("test_id")
                        att_key = f"{test_id}/{att_relpath}"
                        if att_key in attachment_map:
                            test_item_pb.attachment_id = attachment_map[att_key]

                    batch_request.items.append(test_item_pb)

                try:
                    response = retry_on_resource_exhausted(
                        lambda: mpac_service.BatchCreateTestItems(
                            batch_request,
                            metadata=mpac_api_key_metadata,
                        )
                    )()

                    logging.info(
                        f"Batch [{batch_start + 1}-{batch_end}/{total_items}]: "
                        f"{response.successful_count} succeeded, {response.failed_count} failed"
                    )

                    for result in response.results:
                        if result.code != service_pb2.ResponseCode.SUCCESS:
                            logging.error(f"Item {result.id} failed: {result.reason}")

                except Exception as e:
                    logging.error(f"Batch creation failed: {e}")

                gc.collect()

        except Exception as e:
            logging.error(traceback.format_exc())
            logging.error(f"Failed to import test {filename}: {e}")


def create_attachment(file_name, file_path, mpac_api_key_metadata, mpac_service):
    """
    Create an attachment with built-in retry logic for resource exhaustion.
    """
    max_retries = 5
    initial_backoff = 0.1
    backoff = initial_backoff

    for attempt in range(max_retries):
        try:
            attachment_pb = attachment_parser(
                file_name=file_name,
                file_path=file_path,
                owner="mike@pelidum.com",
            )

            if attachment_pb.modality != service_pb2.FileModality.UNSPECIFIED_MODALITY:
                mpac_service.CreateAttachment(
                    request=attachment_pb,
                    metadata=mpac_api_key_metadata,
                )
                return attachment_pb.id

            return None

        except grpc.RpcError as e:
            if (
                e.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
                and attempt < max_retries - 1
            ):
                logging.warning(
                    f"Resource exhausted for {file_name}, retrying in {backoff}s (attempt {attempt + 1}/{max_retries})"
                )
                time.sleep(backoff)
                backoff *= 2
                continue
            else:
                logging.error(f"Error creating attachment {file_name}: {e}")
                raise
        except Exception as e:
            logging.error(f"Error creating attachment {file_name}: {e}")
            return None

    return None


def attachment_parser(
    file_name: str,
    file_path: str,
    owner: str,
) -> service_pb2.FileAttachment:
    attachment_pb = service_pb2.FileAttachment()

    with open(file_path, "rb") as attachment_file_handler:
        file_content = attachment_file_handler.read()

    attachment_pb.id = hashlib.sha256(file_content).hexdigest()
    attachment_filetype_metadata = filetype.guess(file_path)

    if not attachment_filetype_metadata:
        attachment_pb.modality = service_pb2.FileModality.UNSPECIFIED_MODALITY
        return attachment_pb

    attachment_type = attachment_filetype_metadata.mime.split("/")[0]

    match attachment_type:
        case "audio":
            attachment_pb.modality = service_pb2.FileModality.AUDIO
        case "image":
            attachment_pb.modality = service_pb2.FileModality.IMAGE
        case "video":
            attachment_pb.modality = service_pb2.FileModality.VIDEO
        case "_":
            attachment_pb.modality = service_pb2.FileModality.UNSPECIFIED_MODALITY

    if attachment_pb.modality != service_pb2.FileModality.UNSPECIFIED_MODALITY:
        attachment_pb.name = file_name
        attachment_pb.extension = attachment_filetype_metadata.extension
        attachment_pb.file = file_content
        attachment_pb.file_size = math.ceil(len(file_content) / 1024)
        attachment_acls = service_pb2.Visibility()
        attachment_acls.type = (
            service_pb2.Visibility.VisibilityOptions.VISIBILITY_ORG_ONLY
        )
        attachment_pb.visibility.CopyFrom(attachment_acls)
        attachment_pb.owner = owner
        attachment_pb.mime = attachment_filetype_metadata.mime

    return attachment_pb


def should_bootstrap(test_id):
    if FLAGS.bootstrap_include:
        return test_id in FLAGS.bootstrap_include
    return FLAGS.bootstrap


def load_json_data(file_path):
    try:
        if file_path.endswith(".gz"):
            with gzip.open(file_path, "rt") as f:
                return json.load(f)
        else:
            with open(file_path) as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logging.error(f"Failed to load {file_path}: {e}")
        return []


def main(argv):
    del argv

    pd.set_option("display.max_rows", None)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", None)
    pd.set_option("display.max_colwidth", None)

    server_address = f"{FLAGS.mpac_server_host}:{FLAGS.mpac_server_port}"
    logging.info(f"Connecting to MPAC gRPC server at {server_address}...")

    channel_options = [
        ("grpc.max_receive_message_length", mpac_server.MPAC_MAX_MESSAGE_SIZE),
        ("grpc.max_send_message_length", mpac_server.MPAC_MAX_MESSAGE_SIZE),
        ("grpc.http2.max_pings_without_data", 0),
        ("grpc.keepalive_time_ms", 10000),
        ("grpc.keepalive_timeout_ms", 5000),
        ("grpc.keepalive_permit_without_calls", 1),
        ("grpc.http2.min_time_between_pings_ms", 10000),
    ]

    if FLAGS.secure_channel:
        credentials = grpc.ssl_channel_credentials()
        if FLAGS.mpac_cert_file:
            try:
                with open(FLAGS.mpac_cert_file, "rb") as f:
                    trusted_certs = f.read()
            except FileNotFoundError:
                print(f"Error: '{FLAGS.mpac_cert_file}' not found.")
                exit(1)

            credentials = grpc.ssl_channel_credentials(root_certificates=trusted_certs)

        mpac_grpc_channel = grpc.secure_channel(
            server_address,
            credentials=credentials,
            options=channel_options,
        )
    else:
        mpac_grpc_channel = grpc.insecure_channel(
            server_address,
            options=channel_options,
        )

    mpac_service = service_pb2_grpc.MPACStub(mpac_grpc_channel)
    mpac_api_key_metadata = [("x-api-key", FLAGS.mpac_api_key)]

    if FLAGS.bootstrap:
        bootstrap_tests(mpac_api_key_metadata, mpac_service)
        return

    if FLAGS.get_credits:
        logging.info("Fetching available credits / inference gas costs...")
        get_credits_response = mpac_service.GetCredits(
            Empty(),
            metadata=mpac_api_key_metadata,
        )
        print(
            MessageToDict(
                message=get_credits_response,
                always_print_fields_with_no_presence=True,
                preserving_proto_field_name=True,
            )
        )
        return

    match FLAGS.object_type:
        case "models":
            if FLAGS.list:
                logging.info("Listing all available MPAC models...")
                mpac_models = mpac_service.ListModels(
                    service_pb2.ListRequest(),
                    metadata=mpac_api_key_metadata,
                )
                model_list = []
                for mpac_model in mpac_models:
                    print(mpac_model)
                    print("=" * 50)
                    model_list.append(mpac_model.id)

                print(f"\n{len(model_list)} models available:")
                print(model_list)

            if FLAGS.id:
                logging.info(f"Fetching MPAC model ID: {FLAGS.id}")
                model_pb = mpac_service.GetModel(
                    service_pb2.GetRequest(id=FLAGS.id),
                    metadata=mpac_api_key_metadata,
                )
                print(model_pb)

        case "attachments":
            if FLAGS.list:
                logging.info("Listing all available MPAC attachments...")
                for att in mpac_service.ListAttachments(
                    service_pb2.ListRequest(), metadata=mpac_api_key_metadata
                ):
                    print(att)
                    print("=" * 50)

            if FLAGS.id and not FLAGS.delete:
                logging.info(f"Fetching MPAC attachment ID: {FLAGS.id}")
                print(
                    mpac_service.GetAttachment(
                        service_pb2.GetRequest(id=FLAGS.id),
                        metadata=mpac_api_key_metadata,
                    )
                )

            if FLAGS.delete and FLAGS.id:
                print(
                    mpac_service.DeleteAttachment(
                        service_pb2.DeleteRequest(id=FLAGS.id),
                        metadata=mpac_api_key_metadata,
                    )
                )

        case "backends":
            if FLAGS.list:
                logging.info("Listing all available MPAC backends...")
                for backend in mpac_service.ListBackends(
                    service_pb2.ListRequest(), metadata=mpac_api_key_metadata
                ):
                    print(backend)
                    print("=" * 50)

            if FLAGS.id and not FLAGS.delete:
                logging.info(f"Fetching MPAC backend ID: {FLAGS.id}")
                print(
                    mpac_service.GetBackend(
                        service_pb2.GetRequest(id=FLAGS.id),
                        metadata=mpac_api_key_metadata,
                    )
                )

            if FLAGS.create:
                backend_pb = service_pb2.Backend()
                Parse(text=FLAGS.create, message=backend_pb, ignore_unknown_fields=True)
                if len(backend_pb.ListFields()) > 0:
                    print(
                        mpac_service.CreateBackend(
                            backend_pb, metadata=mpac_api_key_metadata
                        )
                    )

            if FLAGS.update:
                backend_pb = service_pb2.Backend()
                Parse(text=FLAGS.update, message=backend_pb, ignore_unknown_fields=True)
                if len(backend_pb.ListFields()) > 0:
                    print(
                        mpac_service.UpdateBackend(
                            backend_pb, metadata=mpac_api_key_metadata
                        )
                    )

            if FLAGS.delete and FLAGS.id:
                print(
                    mpac_service.DeleteBackend(
                        service_pb2.DeleteRequest(id=FLAGS.id),
                        metadata=mpac_api_key_metadata,
                    )
                )

        case "benchmarks":
            if FLAGS.list:
                logging.info("Listing all available MPAC benchmarks...")
                for benchmark in mpac_service.ListBenchmarks(
                    service_pb2.ListRequest(), metadata=mpac_api_key_metadata
                ):
                    print(benchmark)
                    print("=" * 50)

            if FLAGS.id and not FLAGS.delete:
                logging.info(f"Fetching MPAC benchmark ID: {FLAGS.id}")
                print(
                    mpac_service.GetBenchmark(
                        service_pb2.GetRequest(id=FLAGS.id),
                        metadata=mpac_api_key_metadata,
                    )
                )

            if FLAGS.delete and FLAGS.id:
                print(
                    mpac_service.DeleteBenchmark(
                        service_pb2.DeleteRequest(id=FLAGS.id),
                        metadata=mpac_api_key_metadata,
                    )
                )

        case "test_items":
            if FLAGS.list and FLAGS.id:
                logging.info(f"Listing test items for test ID: {FLAGS.id}")
                for item in mpac_service.ListTestItems(
                    service_pb2.ListRequest(parent_id=FLAGS.id),
                    metadata=mpac_api_key_metadata,
                ):
                    print(item)
                    print("=" * 50)

            if FLAGS.delete and FLAGS.id:
                print(
                    mpac_service.DeleteTestItem(
                        service_pb2.DeleteRequest(id=FLAGS.id),
                        metadata=mpac_api_key_metadata,
                    )
                )

        case "test_runs":
            if FLAGS.list:
                logging.info("Listing all available MPAC test runs...")
                for run in mpac_service.ListTestRuns(
                    service_pb2.ListRequest(), metadata=mpac_api_key_metadata
                ):
                    print(run)
                    print("=" * 50)

            if FLAGS.id and not FLAGS.delete:
                logging.info(f"Fetching MPAC test run ID: {FLAGS.id}")
                print(
                    mpac_service.GetTestRun(
                        service_pb2.GetRequest(id=FLAGS.id),
                        metadata=mpac_api_key_metadata,
                    )
                )

            if FLAGS.delete and FLAGS.id:
                print(
                    mpac_service.DeleteTestRun(
                        service_pb2.DeleteRequest(id=FLAGS.id),
                        metadata=mpac_api_key_metadata,
                    )
                )

        case "test_run_answers":
            if FLAGS.list and FLAGS.id:
                logging.info(f"Listing test run answers for run ID: {FLAGS.id}")
                for answer in mpac_service.ListTestRunAnswers(
                    service_pb2.ListRequest(parent_id=FLAGS.id),
                    metadata=mpac_api_key_metadata,
                ):
                    print(answer)
                    print("=" * 50)

            if FLAGS.delete and FLAGS.id:
                print(
                    mpac_service.DeleteTestRunAnswer(
                        service_pb2.DeleteRequest(id=FLAGS.id),
                        metadata=mpac_api_key_metadata,
                    )
                )

        case "tests":
            if FLAGS.list:
                logging.info("Listing all available MPAC tests...")
                for test in mpac_service.ListTests(
                    service_pb2.ListRequest(), metadata=mpac_api_key_metadata
                ):
                    print(test)
                    print("=" * 50)

            if FLAGS.id and not FLAGS.delete:
                logging.info(f"Fetching MPAC test ID: {FLAGS.id}")
                print(
                    mpac_service.GetTest(
                        service_pb2.GetRequest(id=FLAGS.id),
                        metadata=mpac_api_key_metadata,
                    )
                )

            if FLAGS.create:
                test_pb = service_pb2.Test()
                Parse(text=FLAGS.create, message=test_pb, ignore_unknown_fields=True)
                if len(test_pb.ListFields()) > 0:
                    print(
                        mpac_service.CreateTest(test_pb, metadata=mpac_api_key_metadata)
                    )

            if FLAGS.update:
                test_pb = service_pb2.Test()
                Parse(text=FLAGS.update, message=test_pb, ignore_unknown_fields=True)
                if len(test_pb.ListFields()) > 0:
                    print(
                        mpac_service.UpdateTest(test_pb, metadata=mpac_api_key_metadata)
                    )

            if FLAGS.delete and FLAGS.id:
                print(
                    mpac_service.DeleteTest(
                        service_pb2.DeleteRequest(id=FLAGS.id),
                        metadata=mpac_api_key_metadata,
                    )
                )

        case "users":
            if FLAGS.list:
                logging.info("Listing all available MPAC users...")
                mpac_users = mpac_service.ListUsers(
                    service_pb2.ListRequest(),
                    metadata=mpac_api_key_metadata,
                )
                user_list = []
                for mpac_user in mpac_users:
                    print(mpac_user)
                    print("=" * 50)
                    user_list.append(mpac_user.id)

                print(f"{len(user_list)} user(s) in the system:")
                print(user_list)

            if FLAGS.create:
                user_pb = service_pb2.User()
                Parse(
                    text=FLAGS.create,
                    message=user_pb,
                    ignore_unknown_fields=True,
                )
                if len(user_pb.ListFields()) > 0:
                    user_create_response = mpac_service.CreateUser(
                        user_pb,
                        metadata=mpac_api_key_metadata,
                    )
                    print(user_create_response)

            if FLAGS.update:
                user_pb = service_pb2.User()
                Parse(
                    text=FLAGS.update,
                    message=user_pb,
                    ignore_unknown_fields=True,
                )
                if len(user_pb.ListFields()) > 0:
                    user_update_response = mpac_service.UpdateUser(
                        user_pb,
                        metadata=mpac_api_key_metadata,
                    )
                    print(user_update_response)

            if FLAGS.delete and FLAGS.id:
                response_pb = mpac_service.DeleteUser(
                    service_pb2.DeleteRequest(id=FLAGS.id),
                    metadata=mpac_api_key_metadata,
                )
                print(response_pb)

            if FLAGS.id and not FLAGS.delete:
                logging.info(f"Fetching MPAC user ID: {FLAGS.id}")
                user_pb = mpac_service.GetUser(
                    service_pb2.GetRequest(id=FLAGS.id),
                    metadata=mpac_api_key_metadata,
                )
                print(user_pb)

        case _:
            if FLAGS.object_type:
                raise ValueError(
                    f"No action specified for object type {FLAGS.object_type}. "
                    f"Use --list, --get with --id, --create, --update, or --delete with --id."
                )

    mpac_grpc_channel.close()


if __name__ == "__main__":
    logging.use_absl_handler()
    logging.set_verbosity(logging.INFO)
    app.run(main)
