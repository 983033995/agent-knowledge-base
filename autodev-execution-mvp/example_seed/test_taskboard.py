from taskboard import TaskBoard


def test_create_and_get_task():
    board = TaskBoard()
    board.create_task("a", "Write tests")
    assert board.get_task("a")["title"] == "Write tests"


def test_duplicate_task_is_rejected():
    board = TaskBoard()
    board.create_task("a", "Write tests")
    try:
        board.create_task("a", "Duplicate")
    except ValueError:
        pass
    else:
        raise AssertionError("Duplicate task must raise ValueError")


def test_complete_task_sets_done_true():
    board = TaskBoard()
    board.create_task("a", "Write tests")
    assert board.complete_task("a")["done"] is True
