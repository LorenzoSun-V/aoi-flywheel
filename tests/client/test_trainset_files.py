"""The two files fw trainset create reads: the class table and a split file."""
import pytest

from fw import ops


def test_classes(tmp_path):
    p = tmp_path / 'classes.txt'
    p.write_bytes('﻿scratch\r\n凹坑\r\n dent \n\n\n'.encode('utf-8'))
    assert ops.read_classes(p) == ['scratch', '凹坑', 'dent']
    for text, message in (('a\n\nb\n', '第 2 行是空的'), ('\n\n', '是空的'), ('', '是空的')):
        p.write_text(text, encoding='utf-8')
        with pytest.raises(ops.FwError, match=message):
            ops.read_classes(p)
    p.write_bytes('scratch'.encode('utf-16'))
    with pytest.raises(ops.FwError, match='不是 UTF-8'):
        ops.read_classes(p)
    with pytest.raises(ops.FwError, match='读不了'):
        ops.read_classes(tmp_path / 'missing.txt')


def test_split_file(tmp_path):
    p = tmp_path / 'split.txt'
    p.write_text('# made by my script\n'
                 'SES/TX/a.png\ttrain\n'
                 'SES/TX/dir with space/b c.png val\n'
                 'SES\\TX\\2026-10\\d.png   test\n'
                 '\n', encoding='utf-8')
    assert ops.read_split_file(p) == {'SES/TX/a.png': 'train', 'SES/TX/dir with space/b c.png': 'val',
                                      'SES/TX/2026-10/d.png': 'test'}
    for text, message in (('SES/TX/a.png\ttraining\n', '第 1 行'), ('SES/TX/a.png\n', '第 1 行'),
                          ('a.png train\na.png val\n', '出现了两次'), ('# only a comment\n', '是空的')):
        p.write_text(text, encoding='utf-8')
        with pytest.raises(ops.FwError, match=message):
            ops.read_split_file(p)
