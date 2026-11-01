import time

import grpc
from conftest import (
    KNOW_IT_ALL_MODEL_ID,
    MEMBER_ID,
    create_items,
    create_test,
    rpc_code,
    wait_until,
)
from google.protobuf.timestamp_pb2 import Timestamp

from proto import service_pb2


def _start_run(stub, metadata, test_id, models, sample_size):
    replies = list(
        stub.CreateTestRun(
            service_pb2.TestRunRequest(
                test_id=test_id, models=models, sample_size=sample_size
            ),
            metadata=metadata,
        )
    )
    assert len(replies) == 1
    assert replies[0].code == service_pb2.IN_PROGRESS
    assert replies[0].test_id == test_id
    assert replies[0].run_id
    return replies[0].run_id


def _finished_run(stub, metadata, run_id):
    def finished():
        run = stub.GetTestRun(service_pb2.GetRequest(id=run_id), metadata=metadata)
        return run if run.status != service_pb2.IN_PROGRESS else None

    return wait_until(finished)


def test_run_lifecycle_with_know_it_all_model(
    stub, member, outsider, know_it_all_model
):
    test_id = create_test(stub, member)
    create_items(stub, member, test_id, 4)

    run_id = _start_run(stub, member, test_id, [know_it_all_model], sample_size=4)
    run = _finished_run(stub, member, run_id)

    assert run.status == service_pb2.SUCCESS
    assert run.owner == MEMBER_ID
    assert run.test_id == test_id
    assert run.completed_at_utc.seconds >= run.created_at_utc.seconds > 0
    assert len(run.metrics) == 1
    assert run.metrics[0].total == 4
    assert run.metrics[0].num_correct == 4

    answers = list(
        stub.ListTestRunAnswers(
            service_pb2.ListRequest(parent_id=run_id), metadata=member
        )
    )
    assert len(answers) == 4
    assert all(
        a.run_id == run_id and a.is_correct and a.status == service_pb2.TestRunAnswer.OK
        for a in answers
    )
    assert {a.model_id for a in answers} == {KNOW_IT_ALL_MODEL_ID}

    progress = stub.GetRunProgress(service_pb2.GetRequest(id=run_id), metadata=member)
    assert dict(progress.answer_counts) == {KNOW_IT_ALL_MODEL_ID: 4}

    matrix = stub.GetTestRunConfusionMatrix(
        service_pb2.GetRequest(id=run_id), metadata=member
    )
    counts = matrix.per_model[KNOW_IT_ALL_MODEL_ID]
    assert (
        counts.true_positives,
        counts.true_negatives,
        counts.false_positives,
        counts.false_negatives,
    ) == (2, 2, 0, 0)

    runs = list(
        stub.ListTestRuns(service_pb2.ListRequest(parent_id=test_id), metadata=member)
    )
    assert [r.id for r in runs] == [run_id]

    code, _ = rpc_code(stub.UpdateTestRun, run, metadata=outsider)
    assert code == grpc.StatusCode.PERMISSION_DENIED
    code, _ = rpc_code(
        stub.DeleteTestRun, service_pb2.DeleteRequest(id=run_id), metadata=outsider
    )
    assert code == grpc.StatusCode.PERMISSION_DENIED

    finished = stub.GetTestRun(service_pb2.GetRequest(id=run_id), metadata=member)
    run.status = service_pb2.CANCELLED
    run.owner = "spoofed@example.com"
    assert stub.UpdateTestRun(run, metadata=member).code == service_pb2.SUCCESS
    updated = stub.GetTestRun(service_pb2.GetRequest(id=run_id), metadata=member)
    assert updated.owner == MEMBER_ID
    assert updated == finished

    stale = service_pb2.TestRun()
    stale.CopyFrom(finished)
    stale.status = service_pb2.IN_PROGRESS
    del stale.metrics[:]
    stale.total_tokens = 0
    assert stub.UpdateTestRun(stale, metadata=member).code == service_pb2.SUCCESS
    assert (
        stub.GetTestRun(service_pb2.GetRequest(id=run_id), metadata=member) == finished
    )

    assert (
        stub.DeleteTestRun(service_pb2.DeleteRequest(id=run_id), metadata=member).code
        == service_pb2.SUCCESS
    )
    assert stub.GetTestRun(service_pb2.GetRequest(id=run_id), metadata=member).id == ""
    assert (
        list(
            stub.ListTestRunAnswers(
                service_pb2.ListRequest(parent_id=run_id), metadata=member
            )
        )
        == []
    )


def test_default_sample_is_capped_by_item_count(stub, member, debug_model):
    test_id = create_test(stub, member)
    create_items(stub, member, test_id, 3)

    run = _finished_run(
        stub, member, _start_run(stub, member, test_id, [debug_model], sample_size=0)
    )

    assert run.status == service_pb2.SUCCESS
    answers = list(
        stub.ListTestRunAnswers(
            service_pb2.ListRequest(parent_id=run.id), metadata=member
        )
    )
    assert len(answers) == 3


def test_runs_of_hidden_tests_are_hidden(stub, member, outsider, know_it_all_model):
    test_id = create_test(
        stub,
        member,
        visibility=service_pb2.Visibility(
            type=service_pb2.Visibility.VISIBILITY_OWNER_ONLY
        ),
    )
    create_items(stub, member, test_id, 1)
    run_id = _start_run(stub, member, test_id, [know_it_all_model], sample_size=1)
    _finished_run(stub, member, run_id)

    assert all(
        r.id != run_id
        for r in stub.ListTestRuns(service_pb2.ListRequest(), metadata=outsider)
    )
    assert (
        list(
            stub.ListTestRunAnswers(
                service_pb2.ListRequest(parent_id=run_id), metadata=outsider
            )
        )
        == []
    )
    code, _ = rpc_code(
        stub.GetTestRun, service_pb2.GetRequest(id=run_id), metadata=outsider
    )
    assert code == grpc.StatusCode.NOT_FOUND


def _answer_count(stub, metadata, run_id):
    return len(
        list(
            stub.ListTestRunAnswers(
                service_pb2.ListRequest(parent_id=run_id, limit=1000),
                metadata=metadata,
            )
        )
    )


def test_cancelling_mid_run_stops_inference(stub, admin, member):
    backend_id = stub.CreateBackend(
        service_pb2.Backend(
            name="contract-serial-debug",
            backend_type=service_pb2.DEBUG_RANDOM,
            max_concurrency=1,
        ),
        metadata=admin,
    ).id
    try:
        model = next(
            m
            for m in stub.ListModels(service_pb2.ListRequest(), metadata=admin)
            if m.backend_id == backend_id
        )
        total = 60
        test_id = create_test(stub, member)
        create_items(stub, member, test_id, total)
        run_id = _start_run(stub, member, test_id, [model], sample_size=total)
        wait_until(lambda: _answer_count(stub, member, run_id) >= 2)

        run = stub.GetTestRun(service_pb2.GetRequest(id=run_id), metadata=member)
        run.status = service_pb2.CANCELLED
        assert stub.UpdateTestRun(run, metadata=member).code == service_pb2.SUCCESS
        at_cancel = _answer_count(stub, member, run_id)
        time.sleep(3)

        run = stub.GetTestRun(service_pb2.GetRequest(id=run_id), metadata=member)
        assert run.status == service_pb2.CANCELLED
        assert run.completed_at_utc.seconds > 0
        assert at_cancel <= _answer_count(stub, member, run_id) <= at_cancel + 1
        assert _answer_count(stub, member, run_id) < total
    finally:
        stub.DeleteBackend(service_pb2.DeleteRequest(id=backend_id), metadata=admin)


def test_list_runs_honors_limit_and_creation_bounds(stub, member, know_it_all_model):
    test_id = create_test(stub, member)
    create_items(stub, member, test_id, 1)
    before = Timestamp(seconds=int(time.time()) - 1)
    run_ids = {
        _start_run(stub, member, test_id, [know_it_all_model], sample_size=1)
        for _ in range(3)
    }
    after = Timestamp(seconds=int(time.time()) + 2)

    def listed(**fields):
        return [
            r.id
            for r in stub.ListTestRuns(
                service_pb2.ListRequest(parent_id=test_id, **fields), metadata=member
            )
        ]

    assert set(listed()) == run_ids
    assert len(listed(limit=2)) == 2
    assert set(listed(limit=2)) <= run_ids
    assert set(listed(created_after=before)) == run_ids
    assert set(listed(created_after=before, created_before=after)) == run_ids
    assert listed(created_before=before) == []
    assert listed(created_after=after) == []


def test_answer_crud_rpcs_are_unimplemented(stub, member):
    for call, request in [
        (stub.GetTestRunAnswer, service_pb2.GetRequest(id="x")),
        (stub.CreateTestRunAnswer, service_pb2.TestRunRequest()),
        (stub.DeleteTestRunAnswer, service_pb2.DeleteRequest(id="x")),
    ]:
        code, _ = rpc_code(call, request, metadata=member)
        assert code == grpc.StatusCode.UNIMPLEMENTED
