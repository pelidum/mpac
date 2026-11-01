import grpc
from conftest import MEMBER_ID, create_items, create_test, rpc_code, wait_until

from proto import service_pb2


def test_benchmark_lifecycle(stub, member, outsider, know_it_all_model, debug_model):
    test_id = create_test(stub, member)
    create_items(stub, member, test_id, 2)

    replies = list(
        stub.CreateBenchmark(
            service_pb2.BenchmarkRequest(
                test_id=test_id, models=[know_it_all_model, debug_model], sample_size=2
            ),
            metadata=member,
        )
    )
    assert len(replies) == 1
    assert replies[0].code == service_pb2.IN_PROGRESS
    assert replies[0].total_rounds == 1
    benchmark_id = replies[0].benchmark_id

    def finished():
        benchmark = stub.GetBenchmark(
            service_pb2.GetRequest(id=benchmark_id), metadata=member
        )
        return benchmark if benchmark.status != service_pb2.IN_PROGRESS else None

    benchmark = wait_until(finished)
    assert benchmark.status == service_pb2.SUCCESS
    assert benchmark.owner == MEMBER_ID
    assert benchmark.model_count == 2
    assert benchmark.sample_size == 2
    assert benchmark.winner

    listed = [
        b.id for b in stub.ListBenchmarks(service_pb2.ListRequest(), metadata=member)
    ]
    assert benchmark_id in listed

    code, _ = rpc_code(stub.UpdateBenchmark, benchmark, metadata=outsider)
    assert code == grpc.StatusCode.PERMISSION_DENIED
    assert (
        stub.DeleteBenchmark(
            service_pb2.DeleteRequest(id=benchmark_id), metadata=outsider
        ).code
        == service_pb2.ERROR
    )

    benchmark.status = service_pb2.CANCELLED
    assert stub.UpdateBenchmark(benchmark, metadata=member).code == service_pb2.SUCCESS
    assert (
        stub.GetBenchmark(
            service_pb2.GetRequest(id=benchmark_id), metadata=member
        ).status
        == service_pb2.SUCCESS
    )

    assert (
        stub.DeleteBenchmark(
            service_pb2.DeleteRequest(id=benchmark_id), metadata=member
        ).code
        == service_pb2.SUCCESS
    )
    assert (
        stub.GetBenchmark(service_pb2.GetRequest(id=benchmark_id), metadata=member).id
        == ""
    )


def test_benchmark_requires_evaluation_test(
    stub, member, know_it_all_model, debug_model
):
    test_id = create_test(stub, member, type=service_pb2.SURVEY)
    create_items(stub, member, test_id, 1)

    code, _ = rpc_code(
        stub.CreateBenchmark,
        service_pb2.BenchmarkRequest(
            test_id=test_id, models=[know_it_all_model, debug_model]
        ),
        metadata=member,
    )
    assert code != grpc.StatusCode.OK
