# MCP Example (Vagrant)

## 개요
해당 소스는 Infrastructure Provisioning 자동화를 위한 MCP Server 예제입니다.

Streamable HTTP 프로토콜을 통해 다양한 Client에 연결하여 자연어 기반으로 Vagrant(VirtualBox) VM을 생성/변경/삭제/조회할 수 있는 기능을 제공합니다.

Vagrant 기준으로 작성되었으나 `InfraExecutor` 구현체만 추가하면 Terraform, Public Cloud 등 다른 도구로도 확장할 수 있도록 설계되었습니다.

MCP 엔드포인트에는 인증이 없으므로 WSO2 APIM 등 API Gateway와 연동하여 사용하는 것을 권장합니다.

테스트에 필요한 사전 환경 요구사항은 다음과 같습니다.
- 컨테이너 배포용 환경
- Vagrant + VirtualBox 가 설치된 호스트 (MCP Server 에서 SSH 로 접속 가능해야 합니다)
- VM 에 줄 고정 IP 대역 (호스트의 브리지 네트워크, MCP Server 에서 접근 가능해야 합니다)
- MCP 프로토콜을 지원하는 Client (Streamable HTTP 로 연결해야 합니다)

## Architecture 정의
MCP Server에 도구를 쉽게 추가/제거하고 코드 종속성을 줄이기 위해 Modular Monolithic Architecture 및 Microkernel Architecture를 적용하였습니다.

```
MCP Client ──Streamable HTTP(/mcp)──▶ FastAPI + fastapi-mcp
                                         │
                                         ├─ MCP Tools (vg_create_vm / vg_update_vm / vg_destroy_vm / vg_list_vm)
                                         │      ▼
                                         │  ProvisionerRegistry ─▶ VagrantVMProvisioner (ResourceProvisioner 구현체)
                                         │      ├─ VagrantExecutor (InfraExecutor) ─ CommandRunner(SSH/Local) ─▶ vagrant up/reload/destroy
                                         │      └─ AnsibleExecutor ─ ansible-playbook ─▶ VM(고정 IP)
                                         ▼
                                  SQLite (data/infra_state.db: resource)
```

### Interface Architecture

**Resource Provisioner**
MCP Tools와 같은 비즈니스 로직과 인프라 제어 로직(Vagrant/Ansible) 사이의 규격을 정의하는 추상화 인터페이스입니다. 생성/변경/삭제의 상태 전이는 모두 이 클래스가 처리하고, 구현체는 변수 준비(`prepare_data`, `prepare_update`)만 담당합니다.

**InfraExecutor**
리소스를 실제로 만드는 도구의 인터페이스(`apply`, `destroy`)입니다. 현재 구현체는 `VagrantExecutor` 이며, Terraform 등은 같은 인터페이스로 추가할 수 있습니다.

**CommandRunner**
명령을 실행할 위치를 추상화합니다. `SSHRunner`(원격 Vagrant 호스트)와 `LocalRunner`(MCP Server 와 같은 머신)가 있습니다.

**설계 목적**
- 인프라 추상화: Client(User, Agent 등)가 Vagrant, AWS 등 특정 플랫폼에 종속되지 않고 명령을 내릴 수 있도록 격리합니다.
- 작업 일관성: 리소스 종류와 관계 없이 동일한 작업 흐름을 보장합니다.
- 확장성: 새로운 인프라 자산 또는 기능이 추가되어도 기존 소스 코드 수정 없이 새 모듈만 구현하면 즉시 통합할 수 있습니다.

### Sequence Architecture
1. 요청 수신: MCP Client가 MCP Tool을 호출하면, MCP Server가 이를 수신합니다.
2. 리소스 등록: 데이터베이스에 새로운 리소스 정보를 기록하고 상태를 `PENDING`으로 마킹합니다.
3. 자원 할당: `VAGRANT_IP_RANGE` 에서 DB 에 없고 ping 응답이 없는 IP 를 골라 `PROVISIONING` 으로 바꿉니다. 할당에 실패하면 기록을 지우고 오류를 반환합니다.
4. 비동기 실행: Server는 응답을 먼저 반환하고, 백그라운드에서 Vagrantfile 을 렌더링해 호스트의 `{VAGRANT_WORKDIR}/{resource_id}/` 에 쓴 뒤 `vagrant up` 을 실행합니다.
5. 환경 구성: `CONFIGURING` 으로 바꾸고 VM 의 고정 IP 로 Ansible Playbook 을 실행합니다.
6. 완료 처리: 모든 작업이 끝나면 `RUNNING`, 실패하면 `ERROR` 로 바꾸고 원인을 `metadata.last_error` 에 남깁니다.

### Provisioning Workflow

#### 리소스 상태 정의 및 전이 조건

| 상태 (Status) | 의미 (Meaning) | 전이 조건 (Trigger) |
| :--- | :--- | :--- |
| **PENDING** | 생성 요청 수신 | Database 레코드 최초 생성 시 |
| **PROVISIONING** | 인프라 리소스 할당 중 | `vagrant up`(생성) 또는 `vagrant reload`(변경) 시작 시 |
| **CONFIGURING** | SW 및 환경 설정 중 | `vagrant up` 완료 후 Ansible Playbook 시작 시 |
| **RUNNING** | 리소스 사용 가능 | 모든 프로비저닝 로직이 성공적으로 종료되었을 때 |
| **ERROR** | 작업 실패 | 작업 도중 예외 발생, 프로세스 에러 반환, 서버 재시작으로 작업 중단 시 |
| **DESTROYING** | 리소스 삭제 | 리소스 삭제 작업 수행 시 |

- 변경/삭제는 `RUNNING` 또는 `ERROR` 상태에서만 받습니다. (상태 전이는 DB 에서 조건부 UPDATE 로 원자적으로 처리)
- `ERROR` 상태의 리소스는 삭제 후 다시 생성할 수 있습니다.
- 변경 시 IP 등 할당된 값은 유지되고, 요청에 포함된 값(flavor, purpose)만 바뀝니다.

### Database Schema 정의

#### `resource` Table

| 컬럼명 | 타입 | 제약 조건 | 설명 |
| :--- | :--- | :--- | :--- |
| **resource_id** | TEXT | **PK** | 고유 식별자 (예: `vg-sandbox-01`). 호스트의 VM 폴더 이름으로도 쓰입니다 |
| **name** | TEXT | UNIQUE, NOT NULL | 리소스 이름 |
| **provider** | TEXT | NOT NULL | Provider (vg, aws 등) |
| **type** | TEXT | NOT NULL | 리소스 타입 (vm 등) |
| **state** | TEXT | NOT NULL | 리소스 상태 (PENDING, RUNNING 등) |
| **metadata** | TEXT | - | `{"vars": {...Vagrantfile 렌더링 값}, "last_error": "..."}` |
| **created_at** | DATETIME | DEFAULT CURRENT_TIMESTAMP | 생성 시간 |
| **updated_at** | DATETIME | DEFAULT CURRENT_TIMESTAMP | 갱신 시간 |

## 사전 준비

### 1. SSH 키
`secrets/.ssh/` 에 다음 키를 둡니다.

| 파일 | 용도 |
| :--- | :--- |
| `agent_key`, `agent_key.pub` | **필수.** VM 에 공개키를 넣고, Ansible 이 이 키로 VM(`vagrant` 계정)에 접속합니다 |
| `admin_key.pub` | 선택. 관리자 접속용 공개키 (VM 에 함께 등록) |
| `host_key` | 선택. MCP Server 가 Vagrant 호스트에 SSH 로 접속할 때 쓰는 개인키 (`VAGRANT_SSH_KEY` 로 경로 변경 가능) |

```bash
mkdir -p secrets/.ssh
ssh-keygen -t ed25519 -N "" -f secrets/.ssh/agent_key
ssh-keygen -t ed25519 -N "" -f secrets/.ssh/host_key
ssh-copy-id -i secrets/.ssh/host_key.pub <user>@<vagrant-host>
```

### 2. `.env` 작성 (`secrets/.env`)

```
##############
# Vagrant 호스트 접속 (local 이면 MCP Server 와 같은 머신에서 실행)
##############
VAGRANT_HOST="<vagrant-host-address>"
VAGRANT_SSH_USER="<user>"
VAGRANT_SSH_PORT=22
# VAGRANT_SSH_KEY="/app/secrets/.ssh/host_key"
# VAGRANT_SSH_PASSWORD=""
# VAGRANT_SSH_STRICT_HOST_KEY=false

##############
# VM 설정
##############
VAGRANT_WORKDIR="~/vagrant-mcp"        # 호스트에서 VM 폴더를 만들 위치
VAGRANT_IP_RANGE="10.0.0.60-10.0.0.89" # VM 에 줄 고정 IP 대역 (쉼표로 여러 개)
VAGRANT_BRIDGE="eno1"                  # 브리지할 호스트 NIC (비우면 vagrant up 때 물어보므로 꼭 지정)
VAGRANT_BOX="ubuntu/jammy64"
# VAGRANT_BOX_VERSION="20241002.0.0"
# VAGRANT_TIMEOUT=1800                 # vagrant 명령 최대 대기(초)
# VAGRANT_MAX_PARALLEL=2               # 동시에 실행할 vagrant 명령 수
# VAGRANT_SKIP_ANSIBLE=false           # true 면 Ansible 환경 구성을 건너뜀
```

#### 여러 호스트 사용

호스트가 2대 이상이면 `VAGRANT_HOST` 계열 변수 대신 `VAGRANT_HOSTS` 에 JSON 배열을 **한 줄**로 넣습니다. `VAGRANT_HOSTS` 가 있으면 `VAGRANT_HOST`, `VAGRANT_WORKDIR`, `VAGRANT_IP_RANGE`, `VAGRANT_BRIDGE` 는 읽지 않습니다.

```
VAGRANT_HOSTS='[{"name":"host-a","address":"192.168.0.10","user":"oscka","workdir":"/home/oscka/server-workspace","ip_ranges":["192.168.0.60-89"],"bridge":"eno1"},{"name":"host-b","address":"192.168.0.11","user":"oscka","workdir":"/home/oscka/server-workspace","ip_ranges":["192.168.0.90-119"],"bridge":"enp5s0"}]'
```

| 키 | 설명 |
| :--- | :--- |
| `name`, `address` | **필수.** 호스트 이름(`vg_create_vm` 의 `host` 값), 접속 주소 (`local` 이면 같은 머신) |
| `user`, `port` | SSH 계정(기본 `vagrant`), 포트(기본 22) |
| `workdir` | VM 폴더를 만들 경로 (기본 `~/vagrant-mcp`) |
| `default_project` | `vg_create_vm` 에서 `project` 를 비웠을 때 쓸 프로젝트 폴더 (예: `openwebui`). 없으면 `VAGRANT_DEFAULT_PROJECT`, 그것도 없으면 workdir 바로 아래 |
| `ip_ranges` | VM 에 줄 고정 IP 대역 목록. 호스트끼리 겹치지 않게 지정 |
| `bridge` | 브리지할 호스트 NIC 이름 |
| `ssh_key`, `ssh_password` | 호스트별 접속 인증 (없으면 공통 설정) |

- 첫 번째 호스트가 기본 호스트입니다. 호스트 기록이 없는 기존 VM 은 첫 번째 호스트에 있는 것으로 봅니다.
- 호스트가 1대면 `vg_create_vm` 에서 `host` 를 비워도 되고, 2대 이상이면 반드시 지정해야 합니다. 지정하지 않으면 사용 가능한 호스트 목록이 담긴 오류가 반환됩니다.
- VM 이름은 호스트와 상관없이 전체에서 유일해야 합니다. 변경/삭제는 VM 이 생성된 호스트에서 실행됩니다.
- IP 는 선택한 호스트의 `ip_ranges` 에서 DB 에 없고 그 호스트에서 ping 응답이 없는 것을 고릅니다. 동시 실행 수(`VAGRANT_MAX_PARALLEL`)는 호스트마다 적용됩니다.

#### 프로젝트 폴더

VM 은 `{workdir}/{project}/vg-<이름>` 에 만들어집니다. `project` 는 `vg_create_vm` 에서 사용자가 지정하고(영문/숫자/`._-`, 한 단계), 비우면 호스트의 `default_project` → `VAGRANT_DEFAULT_PROJECT` 순으로 씁니다. 둘 다 없으면 `{workdir}/vg-<이름>` 입니다. 생성 때 정해진 project 는 DB 에 기록되어 변경과 삭제가 같은 경로를 쓰므로, 이후에 기본값을 바꿔도 기존 VM 의 경로는 그대로입니다.

#### 응답에서 내부 정보 가리기

LLM(Client)으로 나가는 오류 메시지(호스트 접속 실패, 생성 거절, `last_error`, 오류 응답)는 [redact.py](./src/mcp_server/core/redact.py) 로 내부 정보를 가립니다. 서버 로그와 DB 에는 원문이 남습니다.

| 가리는 것 | 표시 |
| :--- | :--- |
| 호스트 접속 주소(`address` 의 IP/도메인), 그 앞의 `user@` | `<host-addr:호스트이름>`, `***@` |
| 호스트의 `workdir` | `<workdir:호스트이름>` (뒤의 프로젝트/VM 폴더 이름은 그대로) |
| 그 밖의 `/home/<계정>`, `/Users/<계정>` | `/home/***` |
| 호스트의 SSH 계정명 (`vagrant`, `root`, `ubuntu` 등 흔한 이름은 제외) | `***` |
| `password`/`token`/`secret`/`api_key` 뒤의 값, PRIVATE KEY 블록 | `***`, `<PRIVATE KEY 숨김>` |

- `vg_list_hosts` 응답에는 `workdir` 를 넣지 않습니다.
- VM 의 IP 와 호스트 이름은 가리지 않습니다 (사용자에게 알려 줘야 합니다). VM IP 는 LiteLLM 등 앞단에서 가리세요.

#### 생성 전 자원 확인

`vg_create_vm` 은 VM 을 만들기 전에 호스트에 SSH 로 접속해 자원을 확인하고, 부족하면 이유와 그 호스트에서 가능한 사양을 오류로 알려 주며 거절합니다. 기준은 느슨합니다.

| 항목 | 기준 | 설정 |
| :--- | :--- | :--- |
| 메모리 | 가용 메모리(`MemAvailable`) ≥ 요청 사양 + 예비 | `VAGRANT_HOST_MEM_RESERVE_MB` (기본 512) |
| 디스크 | `workdir` 여유 ≥ 최소 여유 | `VAGRANT_MIN_DISK_FREE_MB` (기본 5120) |
| CPU | 확인하지 않음 | |

- 호스트에 접속하지 못하면 생성하지 않고 원인을 알려 줍니다. 접속은 되는데 수치를 읽지 못하는 호스트(`/proc` 이 없는 OS 등)는 막지 않습니다.
- 실행 중인 VM 의 할당 합계(오버커밋)는 따지지 않고, 이 서버가 방금 시작시킨 VM 은 아직 가용 메모리에 반영되지 않을 수 있습니다. 연달아 여러 대를 요청하면 기준을 넘길 수 있습니다.
- `VAGRANT_RESOURCE_CHECK=false` 로 끌 수 있습니다. 변경(`vg_update_vm`)에는 적용되지 않습니다.

flavor 별 사양은 [vm.py](./src/mcp_server/provisioners/vagrant/vm.py) 의 `FLAVOR_SPECS` 에 정의되어 있습니다.

| flavor | vCPU | 메모리 |
| :--- | :--- | :--- |
| small | 1 | 2 GiB |
| medium | 2 | 4 GiB |
| large | 4 | 8 GiB |

## 배포 방법
docker-compose.yml을 통해 배포하여 관리할 수 있습니다.

```bash
docker compose up -d --build
```

MCP Client 에서는 `http://<server>:8000/mcp` 로 연결합니다.

컨테이너 없이 Vagrant 호스트에서 직접 실행할 수도 있습니다 (`VAGRANT_HOST=local`, Linux/macOS).

```bash
pip install -r requirements.txt
cd src && SECRETS_DIR=../secrets python -m mcp_server.main
```

## MCP Tool

| Tool | 설명 |
| :--- | :--- |
| `vg_list_hosts` | 호스트 목록과 자원 현황(CPU, 메모리, 디스크, VirtualBox 실행 현황), 이 서버가 만든 VM 수, 남은 IP 수 조회. 접속 실패 호스트는 `reachable=false` |
| `vg_create_vm` | VM 1대 생성 (name, flavor, purpose, host, project). 호스트가 2대 이상이면 host 지정, project 를 비우면 호스트의 기본 프로젝트. 자원이 부족하면 거절. 백그라운드로 진행 |
| `vg_update_vm` | 사양/용도 변경. 실행 중이면 `vagrant reload` 로 재시작 |
| `vg_destroy_vm` | `vagrant destroy -f` 후 VM 폴더 삭제 |
| `vg_list_vm` | VM 목록, 호스트, 프로젝트, 상태, IP, 사양, 오류 원인 조회 |

```markdown
# User Prompt(VM 생성)
sandbox-01 1대를 생성해줘.

# System
`sandbox-01` VM 생성이 시작되었습니다! (리소스 ID: vg-sandbox-01, 사양: small, IP: 10.0.0.60)

# User Prompt(VM 목록 조회)
현재 vm 목록을 조회해줘.

# System
| VM 이름 | 상태 | 사양 | IP | 생성일 |
|--------|------|------|----|--------|
| sandbox-01 | RUNNING | small | 10.0.0.60 | 2026-10-16 04:40:00 |

# User Prompt(VM 업데이트)
sandbox-01의 사양을 medium으로 변경해줘.

# User Prompt(VM 삭제)
sandbox-01 삭제해줘.
```

## Appendix

### 프로젝트 디렉토리 구조
```
/mcp_example
├─ src/                                  # 컨테이너의 /app 으로 복사됨
│  ├─ mcp_server/                        # MCP/API 소스 코드
│  │  ├─ main.py                        # 실행 엔드포인트 (FastAPI + MCP)
│  │  ├─ database.py                    # SQLite 초기화 및 CRUD 공통 로직
│  │  ├─ utils.py                       # 유틸리티 로직
│  │  ├─ api/                           # 외부 통신 레이어
│  │  │  ├─ mcp/vagrant.py             # MCP Tool 정의
│  │  │  └─ mcp_router.py              # MCP Tool Router
│  │  ├─ core/                          # 프레임워크 코어 로직
│  │  │  ├─ base.py                    # ResourceProvisioner (공통 Lifecycle)
│  │  │  ├─ registry.py                # Provisioner 구현체 Registry
│  │  │  ├─ constants.py               # 전역 상수 및 리소스 상태(Enum)
│  │  │  └─ executor/
│  │  │     ├─ base.py                 # InfraExecutor 인터페이스
│  │  │     ├─ runner.py               # CommandRunner (SSH / Local)
│  │  │     ├─ vagrant.py              # VagrantExecutor
│  │  │     └─ ansible.py              # AnsibleExecutor
│  │  ├─ provisioners/vagrant/vm.py     # Vagrant VM Provisioner (IP 할당, flavor)
│  │  └─ schemas/                       # 입력/메타데이터 스키마
│  ├─ vagrant/templates/Vagrantfile.j2   # VM 별 Vagrantfile 템플릿
│  ├─ ansible/
│  │  ├─ ansible.cfg
│  │  └─ playbooks/
│  │     ├─ group_vars/all.yml
│  │     └─ base.yml                    # 기초 환경 구성 (Debian/Ubuntu)
│  └─ scripts/entrypoint.sh
│
├─ secrets/                              # .env 및 SSH 키 (git 제외)
├─ data/                                 # infra_state.db (git 제외)
├─ requirements.txt
├─ Dockerfile
└─ docker-compose.yml
```
