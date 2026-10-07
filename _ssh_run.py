"""SSH 远程执行助手：python _ssh_run.py "命令"  [超时秒]
长命令自动 nohup 后台化用法：python _ssh_bg.py "命令"  启动 / python _ssh_run.py "tail /tmp/setup.log" 轮询
"""
import sys
import paramiko

HOST, USER, PASS = "42.194.198.198", "lzs", "@Lzs0825"

def connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, password=PASS, timeout=20)
    return c

if __name__ == "__main__":
    cmd = sys.argv[1]
    timeout = int(sys.argv[2]) if len(sys.argv) > 2 else 60
    c = connect()
    _, out, err = c.exec_command(cmd, timeout=timeout)
    o = out.read().decode("utf-8", "replace")
    e = err.read().decode("utf-8", "replace")
    rc = out.channel.recv_exit_status()
    print(o)
    if e.strip():
        print("[stderr]", e[-2000:])
    print(f"[exit {rc}]")
    c.close()
