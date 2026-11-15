"""The texts the server hands to people are formal (CLAUDE.md, 写法): the web shows its errors and notices as they
come, next to its own formal words. A source scan, no database."""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# what the server says to people: every module but the fw-server command line, which only the server's admin
# reads in a terminal; and fw.contract, whose reference checks the server returns as they are
SPOKEN = sorted(set((ROOT / 'server' / 'fwserver').glob('*.py')) - {ROOT / 'server' / 'fwserver' / 'cli.py'}) \
    + [ROOT / 'client' / 'fw' / 'contract.py']
# a colloquial word -> how the pages say it
INFORMAL = {
    '还没': '尚未 / 暂无', '别人': '其他账号', '就行': '即可', '可不写': '选填', '不用': '无需', '你': '您',
    '不了': '无法…', '读不出': '无法读取', '转不成': '无法转换', '删不掉': '无法删除', '打不开': '无法打开',
    '对不上': '不一致', '不能用': '不允许使用 / 无效', '要写': '请填写 / 须写明', '写法不对': '格式不正确',
    '不合格': '格式不正确', '过了': '已…', '不知道': '无法确定', '不认识': '不支持', '不像': '不是有效的',
    '这个': '该', '那个': '该', '跑': '运行', '里有': '中有', '里没有': '中没有', '一样': '相同 / 一致',
    '已经': '已', '要改就': '如需修改', '看有哪些': '查看', '只差': '仅…不同',
}
HAN = re.compile(r'[一-鿿]')


def spoken_texts(path: Path):
    """(line, text) of every string with Chinese in it, docstrings left out."""
    tree = ast.parse(path.read_text(encoding='utf-8'))
    docs = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docs and HAN.search(n.value):
            yield n.lineno, n.value


def test_the_server_speaks_formally():
    found = [f'{path.relative_to(ROOT)}:{line}: 「{word}」应写为「{formal}」：{text}'
             for path in SPOKEN for line, text in spoken_texts(path)
             for word, formal in INFORMAL.items() if word in text]
    assert not found, '\n'.join(found)


def test_the_scan_sees_the_texts_of_the_server():
    texts = [text for path in SPOKEN for _, text in spoken_texts(path)]
    assert len(texts) > 500 and '请先运行 fw login' in ''.join(texts)
    assert not any('Evaluation jobs' in t for t in texts)  # docstrings are not spoken
