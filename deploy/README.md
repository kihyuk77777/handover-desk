# 서버에 올릴 때 쓰는 예시 파일

값(경로, 계정, 도메인, 주소)은 모두 자리 표시이므로 환경에 맞게 바꿔 쓴다.

| 파일 | 용도 |
|---|---|
| `desk-web.service` | 웹 화면 (내부 포트에서만 듣고, 바깥 접속은 웹 서버가 받아 넘긴다) |
| `desk-worker.service` | 작업기 (메일 확인, 모델 작업, 알림, 보안 동향, 기한 알림) |
| `desk-tunnel.service` | 모델이 다른 장비에 있을 때의 SSH 터널. 모델 포트를 열지 않고 연결한다 |
| `apache-vhost.conf` | HTTPS를 받아 내부 포트로 넘기는 설정 |

터널에 쓰는 키는 모델 장비의 `authorized_keys`에서 포트 전달만 허용하도록 제한한다.

```
restrict,port-forwarding,permitopen="127.0.0.1:11434",command="/usr/bin/true" ssh-ed25519 AAAA... desk-tunnel
```
