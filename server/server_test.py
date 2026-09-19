import pytest
import grpc
import time
import sys

from absl import app, flags, logging

from server import service_pb2
from server import service_pb2_grpc
from server import server as mpac_server

from google.protobuf.empty_pb2 import Empty


FLAGS = flags.FLAGS
if "test_server_host" not in FLAGS:
    flags.DEFINE_string(
        "test_server_host", "localhost", "Host address of the test server"
    )

if "test_server_port" not in FLAGS:
    flags.DEFINE_integer("test_server_port", 50051, "Port number of the test server")

if "test_api_key" not in FLAGS:
    flags.DEFINE_string("test_api_key", None, "The API key to use for testing.")

if "test_user" not in FLAGS:
    flags.DEFINE_string(
        "test_user",
        "test@example.com",
        "The user id / email to use for testing. "
        "Must match the --admin_onboarding_id used to start the server.",
    )


@pytest.fixture(scope="session")
def api_key_metadata():
    """Provides the API key metadata for gRPC calls from flags."""
    api_key_info = []

    if FLAGS.test_api_key:
        api_key_info.append(("x-api-key", FLAGS.test_api_key))

    return api_key_info


@pytest.fixture(scope="session")
def mpac_stub():
    """Creates and yields a gRPC stub for the MPAC service from flags."""
    server_address = f"{FLAGS.test_server_host}:{FLAGS.test_server_port}"
    channel = grpc.insecure_channel(
        server_address,
        options=[
            ("grpc.max_receive_message_length", mpac_server.MPAC_MAX_MESSAGE_SIZE)
        ],
    )
    stub = service_pb2_grpc.MPACStub(channel)
    yield stub
    channel.close()


@pytest.fixture(scope="session")
def test_user():
    """Provides a dummy user object for request_owner fields."""
    return service_pb2.User(id=FLAGS.test_user)


def _create_test_test(mpac_stub, api_key_metadata, test_type=service_pb2.EVALUATION):
    """Helper to create a temporary Test for other tests."""
    test_name = f"pytest-helper-test-{int(time.time())}"
    test_to_create = service_pb2.Test(
        name=test_name, description="A helper test", type=test_type
    )
    create_response = mpac_stub.CreateTest(test_to_create, metadata=api_key_metadata)
    assert create_response.code == service_pb2.SUCCESS
    assert create_response.id
    return create_response.id


def _get_test_model(
    mpac_stub, api_key_metadata, test_user, model_id="debug/debug_random_model_1"
):
    """Helper to get a known model PB. Skips test if not found."""
    try:
        model_pb = mpac_stub.GetModel(
            service_pb2.GetRequest(id=model_id),
            metadata=api_key_metadata,
        )
        return model_pb
    except grpc.RpcError as e:
        if e.code() == grpc.StatusCode.NOT_FOUND:
            pytest.skip(f"Test model ID '{model_id}' not found.")
        else:
            raise


### Miscellaneous Service Methods
def test_get_credits(mpac_stub, api_key_metadata):
    """Tests the GetCredits endpoint. Skips if no backends are registered."""
    backends = list(
        mpac_stub.ListBackends(service_pb2.ListRequest(), metadata=api_key_metadata)
    )
    if not backends:
        pytest.skip("No backends registered; skipping GetCredits test.")
    backend_id = backends[0].id
    try:
        response = mpac_stub.GetCredits(
            service_pb2.GetRequest(id=backend_id),
            metadata=api_key_metadata,
        )
        assert response is not None
        assert hasattr(response, "credits_remaining")
    except grpc.RpcError as e:
        if e.code() == grpc.StatusCode.ABORTED:
            pytest.skip(
                f"GetCredits aborted (backend may not support credits): {e.details()}"
            )
        raise


### Models
def test_list_models(mpac_stub, api_key_metadata, test_user):
    """Tests the ListModels endpoint."""
    response = mpac_stub.ListModels(
        service_pb2.ListRequest(),
        metadata=api_key_metadata,
    )
    models = list(response)
    assert models is not None
    assert len(models) >= 0


def test_get_model(mpac_stub, api_key_metadata, test_user):
    """Tests the GetModel endpoint. Uses helper to skip if model not found."""
    known_model_id = "debug/debug_random_model_1"
    model_pb = _get_test_model(mpac_stub, api_key_metadata, test_user, known_model_id)
    assert model_pb is not None
    assert model_pb.id == known_model_id


### Tests
def test_test_crud_lifecycle(mpac_stub, api_key_metadata, test_user):
    """Tests the full Create, Read, Update, and Delete lifecycle for Tests."""
    created_test_id = None
    test_name = f"pytest-test-{int(time.time())}"

    try:
        test_to_create = service_pb2.Test(
            name=test_name,
            description="A test",
            owner=FLAGS.test_user,
            type=service_pb2.EVALUATION,
        )
        create_response = mpac_stub.CreateTest(
            test_to_create,
            metadata=api_key_metadata,
        )
        assert create_response.code == service_pb2.SUCCESS
        created_test_id = create_response.id
        assert created_test_id

        get_response = mpac_stub.GetTest(
            service_pb2.GetRequest(id=created_test_id),
            metadata=api_key_metadata,
        )
        assert get_response is not None
        assert get_response.id == created_test_id
        assert get_response.name == test_name

        list_response = mpac_stub.ListTests(
            service_pb2.ListRequest(),
            metadata=api_key_metadata,
        )
        tests = list(list_response)
        assert any(t.id == created_test_id for t in tests)

        updated_name = f"{test_name}-updated"
        test_to_update = service_pb2.Test()
        test_to_update.CopyFrom(get_response)
        test_to_update.name = updated_name
        update_response = mpac_stub.UpdateTest(
            test_to_update, metadata=api_key_metadata
        )
        assert update_response.code == service_pb2.SUCCESS

        get_updated_response = mpac_stub.GetTest(
            service_pb2.GetRequest(id=created_test_id),
            metadata=api_key_metadata,
        )
        assert get_updated_response.name == updated_name

    finally:
        if created_test_id:
            delete_response = mpac_stub.DeleteTest(
                service_pb2.DeleteRequest(id=created_test_id), metadata=api_key_metadata
            )
            assert delete_response.code == service_pb2.SUCCESS


### TestItems
def test_test_item_crud_lifecycle(mpac_stub, api_key_metadata, test_user):
    """Tests the full Create, Read, Update, and Delete lifecycle for TestItems."""
    created_test_id = None
    created_item_id = None

    try:
        created_test_id = _create_test_test(mpac_stub, api_key_metadata)

        item_to_create = service_pb2.TestItem(
            test_id=created_test_id,
            question="What is 2+2?",
            choices=["1", "2", "4", "8"],
            answer="4",
            is_relevant=True,
        )
        create_response = mpac_stub.CreateTestItem(
            item_to_create, metadata=api_key_metadata
        )
        assert create_response.code == service_pb2.SUCCESS
        created_item_id = create_response.id
        assert created_item_id

        get_response = mpac_stub.GetTestItem(
            service_pb2.GetRequest(id=created_item_id),
            metadata=api_key_metadata,
        )
        assert get_response is not None
        assert get_response.id == created_item_id
        assert get_response.question == "What is 2+2?"

        list_response = mpac_stub.ListTestItems(
            service_pb2.ListRequest(parent_id=created_test_id),
            metadata=api_key_metadata,
        )
        items = list(list_response)
        assert any(i.id == created_item_id for i in items)

        updated_item = service_pb2.TestItem()
        updated_item.CopyFrom(get_response)
        updated_item.question = "What is 2+2? (updated)"
        update_response = mpac_stub.UpdateTestItem(
            updated_item, metadata=api_key_metadata
        )
        assert update_response.code == service_pb2.SUCCESS

    finally:
        if created_item_id:
            mpac_stub.DeleteTestItem(
                service_pb2.DeleteRequest(id=created_item_id), metadata=api_key_metadata
            )
        if created_test_id:
            mpac_stub.DeleteTest(
                service_pb2.DeleteRequest(id=created_test_id), metadata=api_key_metadata
            )


### TestRuns
def test_list_test_runs(mpac_stub, api_key_metadata, test_user):
    """Tests the ListTestRuns endpoint."""
    response = mpac_stub.ListTestRuns(
        service_pb2.ListRequest(), metadata=api_key_metadata
    )
    runs = list(response)
    assert runs is not None
    assert len(runs) >= 0


def test_create_delete_test_run_lifecycle(mpac_stub, api_key_metadata, test_user):
    """Tests the Create, Get, and Delete lifecycle for TestRuns."""
    created_run_id = None
    created_test_id = None
    created_item_id = None

    try:
        created_test_id = _create_test_test(mpac_stub, api_key_metadata)

        item_to_create = service_pb2.TestItem(
            test_id=created_test_id,
            question="What is 1+1?",
            choices=["1", "2", "3"],
            answer="2",
            is_relevant=True,
        )
        item_response = mpac_stub.CreateTestItem(
            item_to_create, metadata=api_key_metadata
        )
        assert item_response.code == service_pb2.SUCCESS
        created_item_id = item_response.id

        model_pb = _get_test_model(mpac_stub, api_key_metadata, test_user)
        test_run_request = service_pb2.TestRunRequest(
            test_id=created_test_id,
            models=[model_pb],
            sample_size=0,
        )

        responses = list(
            mpac_stub.CreateTestRun(test_run_request, metadata=api_key_metadata)
        )

        assert len(responses) > 0
        first_reply = responses[0]
        assert first_reply.code == service_pb2.IN_PROGRESS
        created_run_id = first_reply.run_id
        assert created_run_id

        # CreateTestRun fires inference asynchronously; poll until done.
        for _ in range(30):
            time.sleep(1)
            run_pb = mpac_stub.GetTestRun(
                service_pb2.GetRequest(id=created_run_id),
                metadata=api_key_metadata,
            )
            if run_pb.status != service_pb2.IN_PROGRESS:
                break

        assert run_pb is not None
        assert run_pb.id == created_run_id
        assert run_pb.status == service_pb2.SUCCESS

    finally:
        if created_run_id:
            delete_run_response = mpac_stub.DeleteTestRun(
                service_pb2.DeleteRequest(id=created_run_id), metadata=api_key_metadata
            )
            assert delete_run_response.code == service_pb2.SUCCESS
        if created_item_id:
            mpac_stub.DeleteTestItem(
                service_pb2.DeleteRequest(id=created_item_id), metadata=api_key_metadata
            )
        if created_test_id:
            mpac_stub.DeleteTest(
                service_pb2.DeleteRequest(id=created_test_id), metadata=api_key_metadata
            )


### TestRunAnswers
def test_list_test_run_answers(mpac_stub, api_key_metadata, test_user):
    """Tests the ListTestRunAnswers endpoint."""
    response = mpac_stub.ListTestRunAnswers(
        service_pb2.ListRequest(), metadata=api_key_metadata
    )
    answers = list(response)
    assert answers is not None
    assert len(answers) >= 0


### Attachments
def test_attachment_crud_lifecycle(mpac_stub, api_key_metadata, test_user):
    """Tests the full Create, Read, List, and Delete lifecycle for FileAttachments."""
    created_attachment_id = None

    try:
        attachment_to_create = service_pb2.FileAttachment(
            name="pytest-attachment.txt",
            extension="txt",
            modality=service_pb2.TEXT,
            file=f"pytest attachment content {int(time.time())}".encode(),
            owner=FLAGS.test_user,
        )
        create_response = mpac_stub.CreateAttachment(
            attachment_to_create, metadata=api_key_metadata
        )
        assert create_response.code == service_pb2.SUCCESS
        created_attachment_id = create_response.id
        assert created_attachment_id

        get_response = mpac_stub.GetAttachment(
            service_pb2.GetRequest(id=created_attachment_id),
            metadata=api_key_metadata,
        )
        assert get_response is not None
        assert get_response.id == created_attachment_id
        assert get_response.name == "pytest-attachment.txt"

        list_response = mpac_stub.ListAttachments(
            service_pb2.ListRequest(), metadata=api_key_metadata
        )
        attachments = list(list_response)
        assert any(a.id == created_attachment_id for a in attachments)

    finally:
        if created_attachment_id:
            delete_response = mpac_stub.DeleteAttachment(
                service_pb2.DeleteRequest(id=created_attachment_id),
                metadata=api_key_metadata,
            )
            assert delete_response.code == service_pb2.SUCCESS


### Benchmarks
def test_benchmark_crud_lifecycle(mpac_stub, api_key_metadata, test_user):
    """Tests the Create, Get, List, and Delete lifecycle for Benchmarks."""
    known_test_id = None
    created_item_id = None
    created_benchmark_id = None

    try:
        known_test_id = _create_test_test(mpac_stub, api_key_metadata)

        item_response = mpac_stub.CreateTestItem(
            service_pb2.TestItem(
                test_id=known_test_id,
                question="What is 2+2?",
                choices=["2", "3", "4", "5"],
                answer="4",
                is_relevant=True,
            ),
            metadata=api_key_metadata,
        )
        assert item_response.code == service_pb2.SUCCESS
        created_item_id = item_response.id

        model1_pb = _get_test_model(
            mpac_stub, api_key_metadata, test_user, "debug/debug_random_model_1"
        )
        model2_pb = _get_test_model(
            mpac_stub, api_key_metadata, test_user, "debug/debug_random_model_1"
        )

        if not model1_pb or not model2_pb:
            pytest.skip("Could not retrieve all necessary models for benchmark test.")

        benchmark_request = service_pb2.BenchmarkRequest(
            test_id=known_test_id, models=[model1_pb, model2_pb]
        )

        responses = list(
            mpac_stub.CreateBenchmark(benchmark_request, metadata=api_key_metadata)
        )

        assert len(responses) > 0
        last_reply = responses[-1]
        assert last_reply.code == service_pb2.SUCCESS
        created_benchmark_id = last_reply.benchmark_id
        assert created_benchmark_id

        get_response = mpac_stub.GetBenchmark(
            service_pb2.GetRequest(id=created_benchmark_id),
            metadata=api_key_metadata,
        )
        assert get_response is not None
        assert get_response.id == created_benchmark_id

        list_response = mpac_stub.ListBenchmarks(
            service_pb2.ListRequest(), metadata=api_key_metadata
        )
        benchmarks = list(list_response)
        assert any(b.id == created_benchmark_id for b in benchmarks)

    finally:
        if created_benchmark_id:
            delete_response = mpac_stub.DeleteBenchmark(
                service_pb2.DeleteRequest(id=created_benchmark_id),
                metadata=api_key_metadata,
            )
            assert delete_response.code == service_pb2.SUCCESS
        if created_item_id:
            mpac_stub.DeleteTestItem(
                service_pb2.DeleteRequest(id=created_item_id), metadata=api_key_metadata
            )
        if known_test_id:
            mpac_stub.DeleteTest(
                service_pb2.DeleteRequest(id=known_test_id), metadata=api_key_metadata
            )


### Users
def test_user_crud_lifecycle(mpac_stub, api_key_metadata, test_user):
    """Tests the full Create, Read, Update, and Delete lifecycle for Users."""
    created_user_id = None
    test_email = "test-user@example.com"

    try:
        user_to_create = service_pb2.User(id=test_email, name="Pytest User")
        create_response = mpac_stub.CreateUser(
            user_to_create, metadata=api_key_metadata
        )
        assert create_response.code == service_pb2.SUCCESS
        created_user_id = create_response.id
        assert created_user_id
        assert created_user_id == test_email
        get_response = mpac_stub.GetUser(
            service_pb2.GetRequest(id=created_user_id),
            metadata=api_key_metadata,
        )
        assert get_response is not None
        assert get_response.id == created_user_id
        assert get_response.name == "Pytest User"

        list_response = mpac_stub.ListUsers(
            service_pb2.ListRequest(), metadata=api_key_metadata
        )
        users = list(list_response)
        assert any(u.id == created_user_id for u in users)

        updated_name = "Pytest User (Updated)"
        user_to_update = service_pb2.User()
        user_to_update.CopyFrom(get_response)
        user_to_update.name = updated_name
        update_response = mpac_stub.UpdateUser(
            user_to_update, metadata=api_key_metadata
        )
        assert update_response.code == service_pb2.SUCCESS

        get_updated_response = mpac_stub.GetUser(
            service_pb2.GetRequest(id=created_user_id),
            metadata=api_key_metadata,
        )
        assert get_updated_response.name == updated_name

    finally:
        if created_user_id:
            delete_response = mpac_stub.DeleteUser(
                service_pb2.DeleteRequest(id=created_user_id), metadata=api_key_metadata
            )
            assert delete_response.code == service_pb2.SUCCESS


def main(argv):
    """Main function to run pytest, passing through arguments."""
    pytest_args = [__file__] + argv[1:]
    sys.exit(pytest.main(pytest_args))


if __name__ == "__main__":
    app.run(main)
