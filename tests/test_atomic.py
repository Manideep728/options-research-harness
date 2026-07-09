from bot.atomic import atomic_write_text


def test_write_creates_file_with_content(tmp_path):
    path = tmp_path / "out.json"
    atomic_write_text(str(path), "hello")
    assert path.read_text() == "hello"


def test_write_leaves_no_temp_file_behind(tmp_path):
    path = tmp_path / "out.json"
    atomic_write_text(str(path), "one")
    atomic_write_text(str(path), "two")
    assert path.read_text() == "two"
    assert list(tmp_path.iterdir()) == [path]


def test_write_creates_parent_directories(tmp_path):
    path = tmp_path / "nested" / "dir" / "out.json"
    atomic_write_text(str(path), "data")
    assert path.read_text() == "data"
