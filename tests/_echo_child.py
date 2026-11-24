"""测试用子进程：输出英文提示 → 读一行 → 回显。"""
print("Do you want to proceed?")
try:
    line = input()
except EOFError:
    line = "<EOF>"
print("GOT:[" + line + "]")
