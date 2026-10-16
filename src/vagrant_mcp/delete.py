"""vm_delete 로직 : 점검 -> 1회용 토큰 -> 삭제.

- delete_check : 대상 폴더 검증 + owner 확인 + 사용 흔적 점검 -> ok / caution.
                 owner 가 요청자와 달라도 막지 않고 caution 이유로 알린다.
                 confirm_token(30분, 1회용, host+path 에 묶임)을 준다.
- destroy      : 대상 경로를 다시 검증 -> `vagrant destroy -f` -> VirtualBox 에서 사라졌는지 확인 -> 폴더 삭제.
대상     : {workdir}[/{project}]/{name} 만. 실제 경로(심볼릭 링크 해석)가 workdir 안이어야 한다.
사용 흔적: 실행 중인 머신만 `vagrant ssh --no-tty -c` 로 VM 안에서 본다 (최근 N일 로그인/수정 파일,
           docker, k8s 흔적, 열린 서비스 포트). --no-tty 라 점검 자체는 로그인 기록(wtmp)을 남기지 않는다.
노출     : VM 안 점검 결과는 숫자/시각으로만 파싱한다 (파일명, 계정, IP 원문은 돌려주지 않음).
"""

import base64
import json
import re
import secrets
import shlex
import time
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel

from .config import HostConfig, find_host, get_settings
from .create import _check_project, _vm_dir
from .ssh import run_command
from .vms import MARKER_FILE, OWNER_FILE, OWNER_RE

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_TOKEN_TTL_SEC = 1800

# VM 안에서 실행 (가능하면 sudo). 출력은 KEY=숫자 와 "LOGIN 시각" 줄만.
_PROBE = r"""
export LC_ALL=C
# 열린 서비스 포트 수(LISTEN)에서 뺄 포트. 공백으로 구분해서 앞뒤 공백을 남긴 채 추가한다 (예: " 22 111 ")
SKIP_PORTS=" 22 "
if [ -r /var/log/wtmp ]; then
  echo "WTMP=1"
  # 현재 접속 중인 세션도 last 에 나온다. 계정/출처는 버리고 시각만
  last -R -w --time-format iso 2>/dev/null | awk 'NF>=3 && $1!="reboot" && $1!="shutdown" && $1!="wtmp" {print "LOGIN " $3; if (++n>=500) exit}'
else
  echo "WTMP=0"
fi
P=""
for d in /home /root /opt /srv /var/www /data; do [ -d "$d" ] && P="$P $d"; done
if [ -n "$P" ]; then
  R=$(timeout 15 find $P -xdev -path '*/.cache' -prune -o -type f -mtime -__DAYS__ -printf '%T@\n' 2>/dev/null)
  echo "FILES_RC=$?"
  echo "FILES=$(printf '%s' "$R" | grep -c .)"
fi
if command -v docker >/dev/null 2>&1; then echo "DOCKER=$(docker ps -q 2>/dev/null | grep -c .)"; else echo "DOCKER=0"; fi
LP=" "
if command -v ss >/dev/null 2>&1; then
  LP=" $(ss -Hltn 2>/dev/null | awk '{n=split($4,a,":"); print a[n]}' | sort -u | tr '\n' ' ') "
  # 외부에서 접근 가능한 listen 포트 수. SKIP_PORTS 와 로컬 전용 주소(127.0.0.0/8, ::1)는 빼고, IPv4/IPv6 중복은 포트로 합친다
  echo "LISTEN=$(ss -Hltn 2>/dev/null | awk -v skip="$SKIP_PORTS" '{n=split($4,a,":"); p=a[n]; h=substr($4,1,length($4)-length(p)-1); gsub(/^\[|\]$/,"",h); sub(/%.*/,"",h); if (h ~ /^127\./ || h=="::1" || index(skip, " " p " ")) next; print p}' | sort -u | grep -c .)"
fi
K=0
for p in 6443 10250 9345; do case "$LP" in *" $p "*) K=1 ;; esac; done
for d in /etc/rancher /etc/kubernetes /var/lib/kubelet; do [ -d "$d" ] && K=1; done
echo "K8S=$K"
"""

# 호스트에서 실행. 대상 검증(20~23) 후 마커/owner 와 머신별 상태(+실행 중이면 VM 안 점검)를 출력
_CHECK_SCRIPT = r"""
DIR=__DIR__; WD=__WD__
[ -d "$DIR" ] || exit 20
[ -f "$DIR/Vagrantfile" ] || exit 21
REAL="$(cd "$DIR" && pwd -P)"; WDR="$(cd "$WD" && pwd -P)"
case "$REAL" in "$WDR"/?*) ;; *) exit 22 ;; esac
command -v VBoxManage >/dev/null 2>&1 || exit 23
[ -f "$DIR/__MARKER__" ] && echo "@@MARKER@@"
[ -f "$DIR/__OWNER__" ] && echo "@@OWNER@@ $(head -c 2048 "$DIR/__OWNER__" | base64 -w0)"
for idf in "$DIR"/.vagrant/machines/*/virtualbox/id; do
  [ -f "$idf" ] || continue
  m="$(basename "$(dirname "$(dirname "$idf")")")"
  info="$(VBoxManage showvminfo "$(cat "$idf")" --machinereadable 2>/dev/null </dev/null)"
  st="$(echo "$info" | sed -n 's/^VMState="\(.*\)"$/\1/p')"
  ch="$(echo "$info" | sed -n 's/^VMStateChangeTime="\(.*\)"$/\1/p')"
  echo "@@MACHINE@@ $m|${st:-missing}|$ch"
  if [ "$st" = running ] && [ __PROBE__ = 1 ]; then
    (cd "$DIR" && timeout 60 vagrant ssh "$m" --no-tty -c __PROBE_CMD__ </dev/null 2>/dev/null)
    echo "@@ENDPROBE@@ $?"
  fi
done
true
"""

_DESTROY_SCRIPT = r"""
DIR=__DIR__; WD=__WD__
[ -d "$DIR" ] || exit 20
[ -f "$DIR/Vagrantfile" ] || exit 21
REAL="$(cd "$DIR" && pwd -P)"; WDR="$(cd "$WD" && pwd -P)"
case "$REAL" in "$WDR"/?*) ;; *) exit 22 ;; esac
cd "$REAL" || exit 20
IDS="$(cat .vagrant/machines/*/virtualbox/id 2>/dev/null)"
OUT="$(timeout 240 vagrant destroy -f 2>&1 </dev/null)" || { echo "$OUT" | tail -n 20 >&2; exit 30; }
for id in $IDS; do VBoxManage showvminfo "$id" >/dev/null 2>&1 </dev/null && exit 31; done
cd / && rm -rf -- "$REAL" || exit 32
echo DELETED
"""

_ERRORS = {
    20: "VM 폴더가 없습니다: {path}",
    21: "Vagrantfile 이 없는 폴더는 지우지 않습니다: {path}",
    22: "실제 경로가 workdir 밖입니다 (심볼릭 링크 등). 지우지 않습니다: {path}",
    23: "호스트에 VBoxManage 가 없습니다.",
    30: "vagrant destroy 실패 (폴더는 남겨둠): {stderr}",
    31: "destroy 후에도 VirtualBox 에 VM 이 남아 있습니다 (폴더는 남겨둠).",
    32: "VM 은 지웠지만 폴더 삭제에 실패했습니다: {stderr}",
}

_NO_MARKER = "에이전트가 만든 VM 이 아닐 수 있음 (마커 없음) - 사람이 직접 만든 VM 이면 소유자에게 먼저 확인하세요"


class MachineCheck(BaseModel):
    machine: str
    state: str  # running / poweroff / aborted / saved / missing ...
    state_changed_at: str | None = None  # VirtualBox 마지막 상태 변경 시각
    probed: bool = False  # VM 안 점검을 했는지
    recent_logins: int | None = None  # 최근 window_days 일 로그인 수 (현재 접속 포함)
    last_login_at: str | None = None
    recent_files: int | None = None  # 최근 window_days 일 수정 파일 수 (/home /root /opt /srv /var/www /data)
    docker_containers: int | None = None  # 실행 중인 컨테이너 수
    k8s_active: bool | None = None  # k8s 포트(6443/10250/9345) listen 또는 k8s 디렉터리 존재
    listening_ports: int | None = None  # 외부에서 접근 가능한 listen 포트 수 (22, 로컬 전용 제외)


class DeleteCheck(BaseModel):
    host: str
    project: str | None = None
    name: str
    path: str
    owner: str | None = None  # .owner.json 의 owner (없으면 null = 소유자 기록 없음)
    window_days: int  # 로그인/수정 파일을 "최근 N일" 로 보는 기준
    machines: list[MachineCheck] = []
    verdict: str  # ok / caution(사용자 확인 필요)
    reasons: list[str] = []
    confirm_token: str | None = None
    token_expires_in_sec: int | None = None


class DeleteResult(BaseModel):
    host: str
    name: str
    path: str
    status: str = "deleted"
    confirmed_by: str  # user_typed_name (사용자가 채팅에 VM 이름을 직접 입력)


class _Pending(BaseModel):
    host: str
    path: str
    expires: float


_PENDING: dict[str, _Pending] = {}


def _target(host: str, name: str, project: str | None) -> tuple[HostConfig, str]:
    h = find_host(host)
    if not _NAME_RE.fullmatch(name) or name == "templates":
        raise ValueError(f"잘못된 VM 이름: {name}")
    _check_project(project)
    return h, _vm_dir(h, project, name)


def _run(h: HostConfig, script: str, path: str, timeout: int):
    result = run_command(h, script.replace("__DIR__", shlex.quote(path)).replace("__WD__", shlex.quote(h.workdir.rstrip("/"))), timeout=timeout)
    if result.exit_code != 0:
        msg = _ERRORS.get(result.exit_code, f"exit={result.exit_code}: {{stderr}}")
        raise RuntimeError(msg.format(path=path, stderr=result.stderr.strip()[-300:]))
    return result.stdout


def _parse_iso(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


def _probe_command(days: int) -> str:
    # sudo 가 되면 root 로 (docker 권한, /root 읽기), 아니면 일반 계정으로
    b64 = base64.b64encode(_PROBE.replace("__DAYS__", str(days)).encode()).decode()
    return (
        f"if sudo -n true 2>/dev/null; then echo {b64} | base64 -d | sudo -n bash; "
        f"else echo {b64} | base64 -d | bash; fi"
    )


def _owner_check(owner_b64: str | None, requested_by: str | None) -> tuple[str | None, list[str]]:
    """(owner, caution 이유). 막지 않고 알리기만 한다. 파일이 없으면(기존 VM) 아무것도 알리지 않는다."""
    # requested_by 는 LLM 이 대화에서 채우는 값이라 틀리거나 위조될 수 있다. 보안 경계가 아니라 실수 방지용이다.
    if owner_b64 is None:
        return None, []
    try:
        owner = str(json.loads(base64.b64decode(owner_b64))["owner"]).strip()
    except Exception:
        owner = ""
    if not OWNER_RE.fullmatch(owner):
        return None, [f"{OWNER_FILE} 형식을 읽을 수 없음 (소유자 확인 불가)"]
    if not requested_by:
        return owner, [f"현재 owner 는 {owner} 입니다 (요청자 미지정)"]
    if requested_by.strip().casefold() != owner.casefold():
        return owner, [f"현재 owner 는 {owner} 입니다 (요청자 {requested_by.strip()} 와 다름)"]
    return owner, []


def _parse_kv(lines: list[str]) -> tuple[dict[str, int], list[datetime]]:
    kv: dict[str, int] = {}
    logins: list[datetime] = []
    for line in lines:
        if line.startswith("LOGIN "):
            t = _parse_iso(line[len("LOGIN "):].strip())
            if t:
                logins.append(t)
        else:
            k, sep, v = line.partition("=")
            if sep and v.strip().isdigit():
                kv[k.strip()] = int(v)
    return kv, logins


def _probe_result(mc: MachineCheck, lines: list[str], rc: int | None, days: int, now: datetime) -> list[str]:
    """VM 안 점검 출력을 mc 에 채우고 caution 이유를 돌려준다."""
    n = mc.machine
    kv, logins = _parse_kv(lines)
    if rc != 0 or not {"WTMP", "DOCKER", "K8S"} <= kv.keys():
        return [f"{n}: 실행 중인데 VM 안 점검에 실패함 (vagrant ssh rc={rc})"]
    mc.probed = True
    reasons: list[str] = []
    if kv["WTMP"] == 1:
        mc.recent_logins = sum(1 for t in logins if t > now - timedelta(days=days))
        mc.last_login_at = _iso(max(logins, default=None))
        if mc.recent_logins:
            reasons.append(f"{n}: 최근 {days}일 로그인 {mc.recent_logins}회 (마지막 {mc.last_login_at})")
    else:
        reasons.append(f"{n}: 로그인 기록을 읽을 수 없음")
    if "FILES" in kv:
        mc.recent_files = kv["FILES"]
        if mc.recent_files:
            reasons.append(f"{n}: 최근 {days}일 수정 파일 {mc.recent_files}개")
        if kv.get("FILES_RC") == 124:
            reasons.append(f"{n}: 파일 점검이 시간 초과 (일부만 셈)")
    mc.docker_containers, mc.k8s_active = kv["DOCKER"], kv["K8S"] == 1
    if mc.docker_containers:
        reasons.append(f"{n}: 실행 중인 docker 컨테이너 {mc.docker_containers}개")
    if mc.k8s_active:
        reasons.append(f"{n}: k8s 흔적 있음 (6443/10250/9345 포트 또는 k8s 설정 폴더)")
    if "LISTEN" in kv:
        mc.listening_ports = kv["LISTEN"]
        if mc.listening_ports:
            reasons.append(f"{n}: 열린 서비스 포트 {mc.listening_ports}개 (22/로컬 전용 제외)")
    return reasons


def _parse_check(stdout: str, days: int) -> tuple[bool, str | None, list[MachineCheck], list[str]]:
    """(마커 있음, owner base64, 머신 목록, caution 이유)."""
    now = datetime.now(timezone.utc)
    marker, owner_b64, machines, caution = False, None, [], []
    probe: list[str] = []
    for line in stdout.splitlines():
        if line == "@@MARKER@@":
            marker = True
        elif line.startswith("@@OWNER@@ "):
            owner_b64 = line.split(" ", 1)[1].strip()
        elif line.startswith("@@MACHINE@@ "):
            name, state, changed = (line.split(" ", 1)[1].split("|", 2) + ["", ""])[:3]
            mc = MachineCheck(machine=name, state=state, state_changed_at=_iso(_parse_iso(changed)) if changed else None)
            machines.append(mc)
            probe = []
            if state != "running":
                when = f" (마지막 상태 변경: {mc.state_changed_at})" if mc.state_changed_at else ""
                caution.append(f"{name}: 꺼져 있어({state}) 안을 점검할 수 없음{when}")
        elif line.startswith("@@ENDPROBE@@ ") and machines:
            rc = line.split()[-1]
            caution += _probe_result(machines[-1], probe, int(rc) if rc.isdigit() else None, days, now)
        else:
            probe.append(line)
    return marker, owner_b64, machines, caution


def _inspect(h: HostConfig, path: str, days: int, probe: bool) -> str:
    script = (
        _CHECK_SCRIPT.replace("__MARKER__", MARKER_FILE)
        .replace("__OWNER__", OWNER_FILE)
        .replace("__PROBE__", "1" if probe else "0")
        .replace("__PROBE_CMD__", shlex.quote(_probe_command(days)))
    )
    return _run(h, script, path, timeout=180)


def delete_check(host: str, name: str, project: str | None = None, requested_by: str | None = None) -> DeleteCheck:
    h, path = _target(host, name, project)
    days = get_settings().delete_window_days
    marker, owner_b64, machines, caution = _parse_check(_inspect(h, path, days, probe=True), days)
    owner, owner_notes = _owner_check(owner_b64, requested_by)
    if not marker:
        caution.insert(0, _NO_MARKER)
    reasons = owner_notes + caution
    now = time.time()
    for k, p in list(_PENDING.items()):
        if p.expires < now:
            _PENDING.pop(k, None)
    token = secrets.token_urlsafe(16)
    _PENDING[token] = _Pending(host=h.name, path=path, expires=now + _TOKEN_TTL_SEC)
    return DeleteCheck(host=h.name, project=project, name=name, path=path, owner=owner, window_days=days,
                       machines=machines, verdict="caution" if reasons else "ok", reasons=reasons,
                       confirm_token=token, token_expires_in_sec=_TOKEN_TTL_SEC)


def take_token(token: str, host: str, name: str, project: str | None) -> _Pending:
    """토큰을 꺼낸다 (1회용). 대상이 다르거나 만료면 에러."""
    h, path = _target(host, name, project)
    pending = _PENDING.pop(token, None)
    if pending is None or pending.expires < time.time():
        raise ValueError("confirm_token 이 없거나 만료되었습니다. vm_delete_check 부터 다시 하세요.")
    if pending.host != h.name or pending.path != path:
        raise ValueError("confirm_token 이 다른 VM 의 것입니다. vm_delete_check 부터 다시 하세요.")
    return pending


def destroy(pending: _Pending, host: str, name: str, project: str | None, confirmed_by: str) -> DeleteResult:
    """대상 경로만 다시 검증하고 destroy + 폴더 삭제. (owner/마커/사용 흔적은 사용자가 이미 확인한 것)"""
    h, path = _target(host, name, project)
    if "DELETED" not in _run(h, _DESTROY_SCRIPT, path, timeout=300):
        raise RuntimeError("삭제 결과를 확인하지 못했습니다 (DELETED 표시 없음).")
    return DeleteResult(host=h.name, name=name, path=path, confirmed_by=confirmed_by)
