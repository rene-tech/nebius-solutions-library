import pytest
from observe_native import parse_args, task_selector


def test_existing_sampler_default_remains_exact_owned_task():
    args = parse_args(["--output", "/tmp/unused-native-observer"])
    assert task_selector(args.task_label) == "scientific-ai.nebius.com/task=lynx-performance-20261005"
    assert args.interval == 10


def test_rdma_sampler_is_explicit_and_cannot_expand_to_customer_labels():
    args = parse_args(["--output", "/tmp/unused-native-observer", "--task-label", "lynx-rdma-20261005"])
    assert task_selector(args.task_label) == "scientific-ai.nebius.com/task=lynx-rdma-20261005"
    with pytest.raises(ValueError, match="owned native"):
        task_selector("customer")
    with pytest.raises(SystemExit):
        parse_args(["--output", "/tmp/unused-native-observer", "--task-label", "customer"])
