import grpc
import pytest
from conftest import MEMBER_ID, OUTSIDER_ID, create_items, create_test, rpc_code

from proto import service_pb2

OWNER_ONLY = service_pb2.Visibility(type=service_pb2.Visibility.VISIBILITY_OWNER_ONLY)
PUBLIC = service_pb2.Visibility(type=service_pb2.Visibility.VISIBILITY_PUBLIC)


def test_test_crud_lifecycle(stub, member):
    test_id = create_test(stub, member, id="client-chosen", owner="spoofed@example.com")
    assert test_id != "client-chosen"

    test = stub.GetTest(service_pb2.GetRequest(id=test_id), metadata=member)
    assert test.owner == MEMBER_ID
    assert test.created_at_utc.seconds > 0
    assert any(
        t.id == test_id
        for t in stub.ListTests(service_pb2.ListRequest(), metadata=member)
    )

    test.name = "renamed"
    test.owner = "spoofed@example.com"
    assert stub.UpdateTest(test, metadata=member).code == service_pb2.SUCCESS
    updated = stub.GetTest(service_pb2.GetRequest(id=test_id), metadata=member)
    assert updated.name == "renamed"
    assert updated.owner == MEMBER_ID

    assert (
        stub.DeleteTest(service_pb2.DeleteRequest(id=test_id), metadata=member).code
        == service_pb2.SUCCESS
    )
    code, _ = rpc_code(
        stub.GetTest, service_pb2.GetRequest(id=test_id), metadata=member
    )
    assert code != grpc.StatusCode.OK


def test_owner_only_tests_are_hidden_from_others(stub, admin, member, outsider):
    test_id = create_test(stub, member, visibility=OWNER_ONLY)

    code, _ = rpc_code(
        stub.GetTest, service_pb2.GetRequest(id=test_id), metadata=outsider
    )
    assert code != grpc.StatusCode.OK
    assert all(
        t.id != test_id
        for t in stub.ListTests(service_pb2.ListRequest(), metadata=outsider)
    )
    assert (
        stub.GetTest(service_pb2.GetRequest(id=test_id), metadata=admin).id == test_id
    )


def test_specified_users_visibility(stub, member, outsider):
    test_id = create_test(
        stub,
        member,
        visibility=service_pb2.Visibility(
            type=service_pb2.Visibility.VISIBILITY_SPECIFIED_USERS,
            allowed_users=[OUTSIDER_ID],
        ),
    )
    assert (
        stub.GetTest(service_pb2.GetRequest(id=test_id), metadata=outsider).id
        == test_id
    )


V = service_pb2.Visibility


@pytest.mark.parametrize(
    "visibility, owner_sees, outsider_sees",
    [
        (None, True, True),
        (V(type=V.VISIBILITY_UNSET), True, True),
        (V(type=V.VISIBILITY_OWNER_ONLY), True, False),
        (V(type=V.VISIBILITY_ORG_ONLY), True, True),
        (V(type=V.VISIBILITY_SPECIFIED_USERS, allowed_users=[OUTSIDER_ID]), True, True),
        (
            V(type=V.VISIBILITY_SPECIFIED_USERS, allowed_users=["x@example.com"]),
            True,
            False,
        ),
        (V(type=V.VISIBILITY_PUBLIC), True, True),
        (V(type=V.VISIBILITY_ADMIN), False, False),
    ],
    ids=[
        "absent",
        "unset",
        "owner_only",
        "org_only",
        "specified_with_user",
        "specified_without_user",
        "public",
        "admin",
    ],
)
def test_visibility_modes(
    stub, admin, member, outsider, visibility, owner_sees, outsider_sees
):
    fields = {} if visibility is None else {"visibility": visibility}
    test_id = create_test(stub, member, **fields)

    def sees(metadata):
        listed = any(
            t.id == test_id
            for t in stub.ListTests(
                service_pb2.ListRequest(limit=1000), metadata=metadata
            )
        )
        code, _ = rpc_code(
            stub.GetTest, service_pb2.GetRequest(id=test_id), metadata=metadata
        )
        assert listed == (code == grpc.StatusCode.OK)
        return listed

    assert sees(admin)
    assert sees(member) == owner_sees
    assert sees(outsider) == outsider_sees


def test_list_tests_honors_limit(stub, member):
    for _ in range(3):
        create_test(stub, member)

    assert (
        len(list(stub.ListTests(service_pb2.ListRequest(limit=2), metadata=member)))
        == 2
    )
    assert (
        len(list(stub.ListTests(service_pb2.ListRequest(limit=1), metadata=member)))
        == 1
    )


def test_only_owner_or_admin_can_modify_tests(stub, admin, member, outsider):
    test_id = create_test(stub, member, visibility=PUBLIC)
    test = stub.GetTest(service_pb2.GetRequest(id=test_id), metadata=outsider)

    code, _ = rpc_code(stub.UpdateTest, test, metadata=outsider)
    assert code == grpc.StatusCode.PERMISSION_DENIED
    code, _ = rpc_code(
        stub.DeleteTest, service_pb2.DeleteRequest(id=test_id), metadata=outsider
    )
    assert code == grpc.StatusCode.PERMISSION_DENIED

    assert (
        stub.DeleteTest(service_pb2.DeleteRequest(id=test_id), metadata=admin).code
        == service_pb2.SUCCESS
    )


def test_test_item_crud_lifecycle(stub, member):
    test_id = create_test(stub, member)
    (item_id,) = create_items(stub, member, test_id, 1)

    item = stub.GetTestItem(service_pb2.GetRequest(id=item_id), metadata=member)
    assert item.test_id == test_id
    assert list(item.choices) == ["yes", "no"]
    assert (
        stub.GetTest(service_pb2.GetRequest(id=test_id), metadata=member).item_count
        == 1
    )

    listed = list(
        stub.ListTestItems(service_pb2.ListRequest(parent_id=test_id), metadata=member)
    )
    assert [i.id for i in listed] == [item_id]

    item.question = "updated?"
    assert stub.UpdateTestItem(item, metadata=member).code == service_pb2.SUCCESS
    assert (
        stub.GetTestItem(service_pb2.GetRequest(id=item_id), metadata=member).question
        == "updated?"
    )

    assert (
        stub.DeleteTestItem(service_pb2.DeleteRequest(id=item_id), metadata=member).code
        == service_pb2.SUCCESS
    )
    assert (
        list(
            stub.ListTestItems(
                service_pb2.ListRequest(parent_id=test_id), metadata=member
            )
        )
        == []
    )
    assert (
        stub.GetTest(service_pb2.GetRequest(id=test_id), metadata=member).item_count
        == 0
    )


def test_items_require_test_ownership(stub, member, outsider):
    test_id = create_test(stub, member, visibility=PUBLIC)
    (item_id,) = create_items(stub, member, test_id, 1)

    reply = stub.CreateTestItem(
        service_pb2.TestItem(test_id=test_id, question="?"), metadata=outsider
    )
    assert reply.code == service_pb2.ERROR
    item = stub.GetTestItem(service_pb2.GetRequest(id=item_id), metadata=outsider)
    code, _ = rpc_code(stub.UpdateTestItem, item, metadata=outsider)
    assert code == grpc.StatusCode.PERMISSION_DENIED
    code, _ = rpc_code(
        stub.DeleteTestItem, service_pb2.DeleteRequest(id=item_id), metadata=outsider
    )
    assert code == grpc.StatusCode.PERMISSION_DENIED


def test_items_of_hidden_tests_are_hidden(stub, member, outsider):
    test_id = create_test(stub, member, visibility=OWNER_ONLY)
    (item_id,) = create_items(stub, member, test_id, 1)

    assert (
        stub.GetTestItem(service_pb2.GetRequest(id=item_id), metadata=outsider).id == ""
    )


def test_batch_create_reports_per_item_results(stub, member, outsider):
    mine = create_test(stub, member)
    theirs = create_test(stub, outsider)

    reply = stub.BatchCreateTestItems(
        service_pb2.BatchCreateTestItemsRequest(
            items=[
                service_pb2.TestItem(
                    test_id=mine, question="a", choices=["x"], answer="x"
                ),
                service_pb2.TestItem(
                    test_id=mine, question="b", choices=["x"], answer="x"
                ),
                service_pb2.TestItem(
                    test_id=theirs, question="c", choices=["x"], answer="x"
                ),
            ]
        ),
        metadata=member,
    )

    assert reply.successful_count == 2
    assert reply.failed_count == 1
    assert [r.code for r in reply.results].count(service_pb2.SUCCESS) == 2
    listed = list(
        stub.ListTestItems(service_pb2.ListRequest(parent_id=mine), metadata=member)
    )
    assert sorted(i.question for i in listed) == ["a", "b"]


def test_item_updates_authorize_against_the_stored_parent(
    stub, admin, member, outsider
):
    victim_test = create_test(stub, member, visibility=PUBLIC)
    (victim_item,) = create_items(stub, member, victim_test, 1)
    attacker_test = create_test(stub, outsider)

    hijack = service_pb2.TestItem(
        id=victim_item, test_id=attacker_test, question="pwned"
    )
    code, _ = rpc_code(stub.UpdateTestItem, hijack, metadata=outsider)

    assert code == grpc.StatusCode.PERMISSION_DENIED
    stored = stub.GetTestItem(service_pb2.GetRequest(id=victim_item), metadata=member)
    assert (stored.test_id, stored.question) == (victim_test, "Is 0 even?")


def test_owner_can_move_items_between_owned_tests(stub, member, outsider):
    source = create_test(stub, member)
    target = create_test(stub, member)
    foreign = create_test(stub, outsider, visibility=PUBLIC)
    (item_id,) = create_items(stub, member, source, 1)
    item = stub.GetTestItem(service_pb2.GetRequest(id=item_id), metadata=member)

    item.test_id = foreign
    code, _ = rpc_code(stub.UpdateTestItem, item, metadata=member)
    assert code == grpc.StatusCode.PERMISSION_DENIED

    item.test_id = target
    assert stub.UpdateTestItem(item, metadata=member).code == service_pb2.SUCCESS
    counts = [
        stub.GetTest(service_pb2.GetRequest(id=t), metadata=member).item_count
        for t in (source, target)
    ]
    assert counts == [0, 1]
