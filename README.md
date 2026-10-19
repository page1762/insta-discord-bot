# 인스타 → 디스코드 알림 봇

지정한 인스타 계정에 새 게시물/릴스가 올라오면 디스코드 채널에 썸네일이 붙은 알림을 보냅니다.
GitHub Actions 가 10분마다 자동 실행하므로 PC 를 켜 둘 필요가 없습니다.

## 구성

| 파일 | 역할 |
|---|---|
| `insta_discord_bot.py` | 봇 본체 |
| `.github/workflows/insta-discord.yml` | 10분마다 실행 설정 |
| `state.json` | 이미 알린 게시물 기록 (자동 갱신) |
| Secret `CONFIG_JSON` | 웹훅·토큰 등 비밀 설정 (저장소에 공개되지 않음) |

## 설정 바꾸기

알림 채널·대상 계정·토큰을 바꾸려면:
1. PC 의 instabot 폴더에서 `config.json` 수정 (토큰 갱신은 `python insta_discord_bot.py --setup`)
2. 저장소 **Settings → Secrets and variables → Actions → CONFIG_JSON → 수정(연필)** 에
   `config.json` 내용 전체를 붙여 넣고 저장

## 수동 실행 / 테스트

**Actions → insta-discord → Run workflow**
- `test` 체크: 최신 게시물 1개를 바로 전송
- 체크 안 함: 일반 확인 1회

## 공식 API 토큰 (Business Discovery)

권한: `instagram_basic`, `instagram_manage_insights`, `pages_show_list`, `pages_read_engagement`, `business_management`, `ads_read`
그래프 API 탐색기에서 토큰 발급 → PC 에서 `python insta_discord_bot.py --setup` → 위 방법으로 Secret 갱신.
`--setup` 중 "사용자 토큰(60일)" 문구가 나오면 60일 안에 같은 과정을 반복해야 합니다.
