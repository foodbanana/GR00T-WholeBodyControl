# legacy — 사용하지 않는 카메라 서버 버전들

여기 있는 파일은 **어느 것도 현재 파이프라인에서 실행되지 않는다.**
데이터 수집에 실제로 쓰는 것은 상위 `docker/` 의 v8뿐이다.

| 현재 사용 | 파일 |
|---|---|
| 이미지 | `ltw-camera-server:1.1-foxy-3cam` |
| Dockerfile | `docker/Dockerfile.ltw_camera_server_ros2foxy_v8` |
| 실행 스크립트 | `docker/run_ltw_camera_server_ros2foxy_v8.sh` |
| 롤백용 (v7) | `docker/Dockerfile.ltw_camera_server_ros2foxy_v7`, `docker/run_ltw_camera_server_ros2foxy_v7.sh` |

운영 절차는 리포 루트의 [data_collection.md](../../data_collection.md) 를 볼 것.

---

## 여기 있는 파일

| 파일 | 이미지 태그 | 비고 |
|---|---|---|
| `Dockerfile.ltw_camera_server_ros2foxy_v6` | `0.9-foxy-3cam` | 3-cam 초기 버전 (머리 = videohub RPC) |
| `Dockerfile.ltw_camera_server_ros2foxy_v5` | `0.8-foxy-videoclient` | 1-cam, VideoClient RPC |
| `Dockerfile.ltw_camera_server_ros2foxy_v4` | `0.7-foxy-cdds0102-rmw` | ROS 2 Foxy + CycloneDDS rmw |
| `Dockerfile.ltw_camera_server_ros2foxy_v3` | `0.6-foxy-cdds0102` | CycloneDDS 0.10.2 |
| `Dockerfile.ltw_camera_server_ros2foxy_v2` | `0.4-foxy` | |
| `Dockerfile.ltw_camera_server_ros2foxy` | `0.4-foxy` | |
| `Dockerfile.ltw_camera_server_ros2` | `0.3-ros2` | |
| `Dockerfile.ltw_camera_server_dds` | `0.2-dds` | |
| `Dockerfile.ltw_camera_server` | `0.1-rgb` | |
| `Dockerfile copy.ltw_camera_server_backup_260702` | `0.1-rgb` | 백업본 |
| `Dockerfile.camera_server` | — | |
| `run_ltw_camera_server_ros2foxy_v6.sh` | | v6 실행 스크립트 |
| `run_ltw_camera_server_ros2foxy.sh` | | 0.8-foxy-videoclient 실행 스크립트 |
| `run_ltw_camera_server.sh` | | |
| `run_camera_server.sh` | | |
| `camera_subscriber.py` | | ROS 2 `/frontvideostream` → ZMQ. v0.4~v0.7 시절의 컨테이너 내부 로직 |
| `camera_forwarder.py` | | v5 CMD가 쓰던 머리 전용 1-cam forwarder(videohub RPC). 머리만 확인하는 용도는 현재 `camera_forwarder_3cam.py --no-wrists` 로 대체됨 |
| `vendor/unitree_go/` | | `Go2FrontVideoData` ROS 2 메시지 패키지. v2~v5만 `COPY` 한다 |
| `cyclonedds.xml.bak_v2_0.7syntax` | | 구 문법 CycloneDDS 설정 백업 |
| `README.ltw_camera_server_ros2foxy.md` | | `camera_subscriber.py` 기반 `0.4-foxy` 시절 문서 |
| `RUNBOOK.3cam_realsense.md` | | 구 운영 런북. **머리 시리얼이 옛 값으로 박혀 있으니 이대로 따라하지 말 것.** 운영 절차는 [data_collection.md](../../data_collection.md) 로 일원화됨 |

## 다시 빌드하려면

이 Dockerfile들은 `COPY src/...`, `COPY config/...` 를 **상위 `docker/` 기준**으로 참조한다.
빌드 컨텍스트를 `docker/` 로 주고 `-f` 로 이 디렉터리의 파일을 지정해야 한다.

```bash
docker build -f docker/legacy/<Dockerfile> -t <tag> docker/
```

`camera_subscriber.py` 를 COPY하는 버전(v1~v4)은 이 파일이 `docker/src/` 에서
여기로 옮겨졌으므로 COPY 경로를 고쳐야 빌드된다.
