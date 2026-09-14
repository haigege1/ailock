"""QtApp 接线自检（静态分析，不启动应用）。

    python tools/test_qt_app_wiring.py

为什么要这个测试：
    ui/qt/app.py 曾经出现过「调用了不存在的方法」—— `_start_net_watch()`
    和 `_power_selfcheck()` 被 __init__ 调用，但类里根本没有定义，
    `self.net_watch` 也从没初始化。因为跑的是打包好的旧 exe，问题一直没
    暴露，直到要重新打包才发现源码起不来。这类错误在单元测试里很难覆盖
    （不实例化就走不到），所以直接用 AST 静态扫一遍：

  1. 类里 self.xxx() 调用的私有方法必须都已定义
  2. 信号 connect 的槽必须已定义
  3. 类里读到的 self.<属性> 必须在 __init__ 里赋过初值
  4. 电源模块对外接口完整（PowerGuard / PowerRequest / MonitorOff）
"""
import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

failures = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        failures.append(name)


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_PY = os.path.join(ROOT, "ui", "qt", "app.py")

with open(APP_PY, encoding="utf-8") as f:
    tree = ast.parse(f.read())

cls = [n for n in tree.body
       if isinstance(n, ast.ClassDef) and n.name == "QtApp"][0]
defined = {n.name for n in cls.body if isinstance(n, ast.FunctionDef)}
init = [n for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "__init__"][0]

# ---------------------------------------------------------------- 1. 方法调用
called = set()
for node in ast.walk(cls):
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"):
        called.add(node.func.attr)
missing_methods = sorted(m for m in called if m.startswith("_")
                         and m not in defined)
check("1. self._xxx() 调用的方法都已定义", not missing_methods,
      f"missing={missing_methods}")

# ---------------------------------------------------------------- 2. 信号槽
slots = set()
for node in ast.walk(cls):
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "connect"
            and isinstance(node.func.value, ast.Attribute)
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "self"):
        for arg in node.args:
            if (isinstance(arg, ast.Attribute)
                    and isinstance(arg.value, ast.Name)
                    and arg.value.id == "self"):
                slots.add(arg.attr)
missing_slots = sorted(s for s in slots if s not in defined)
check("2. 信号 connect 的槽都已定义", not missing_slots,
      f"missing={missing_slots}")

# ---------------------------------------------------------------- 3. 属性初值
def _assigned_in(nodes) -> set:
    """收集赋值语句里的 self.X，兼容 `x = 1` 与 `x: T = 1` 两种写法。"""
    # 传进来的是语句列表，包一层 Module 才能用 ast.walk 递归下去
    tree = ast.Module(body=list(nodes), type_ignores=[])
    out = set()
    for node in ast.walk(tree):
        targets = []
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        for t in targets:
            if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                    and t.value.id == "self"):
                out.add(t.attr)
    return out


# __init__ 里的实例属性 + 类体里的类属性
# （Signal 是 `statusEvent = Signal(...)` —— 类体里的裸名赋值，self.X 同样能取到）
class_attrs = {t.id for node in cls.body if isinstance(node, ast.Assign)
               for t in node.targets if isinstance(t, ast.Name)}
assigned = _assigned_in(init.body) | _assigned_in(cls.body) | class_attrs

used = set()
for fn in cls.body:
    if not isinstance(fn, ast.FunctionDef) or fn.name == "__init__":
        continue
    for node in ast.walk(fn):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id == "self"):
            used.add(node.attr)
uninitialized = sorted(a for a in used
                       if a not in assigned and a not in defined
                       and not a.startswith("__"))
check("3. 方法中读到的 self.<属性> 都在 __init__ 赋初值",
      not uninitialized, f"uninitialized={uninitialized}")

# ---------------------------------------------------------------- 4. 电源接口
import core.power as P  # noqa: E402

check("4. PowerGuard 可启动/停止",
      hasattr(P.PowerGuard, "start") and hasattr(P.PowerGuard, "stop"))
check("5. PowerRequest 提供 system/execution/display",
      all(hasattr(P.PowerRequest, m)
          for m in ("open", "close", "system", "execution", "display")))
check("6. MonitorOff 提供 turn_off", hasattr(P.MonitorOff, "turn_off"))
check("7. 电源自检接口存在", hasattr(P, "check_environment")
      and hasattr(P, "evaluate_selfcheck"))
check("8. 进程级请求类型常量齐全",
      (P.PowerRequestSystemRequired, P.PowerRequestExecutionRequired,
       P.PowerRequestDisplayRequired) == (1, 3, 0),
      f"{P.PowerRequestSystemRequired}/{P.PowerRequestExecutionRequired}"
      f"/{P.PowerRequestDisplayRequired}")

print()
if failures:
    print(f"结果：{len(failures)} 项失败 -> {failures}")
    sys.exit(1)
print("结果：全部通过 ✓")
