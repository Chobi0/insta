# IGSaver — 인스타 릴스/스토리 원본 화질 다운로더

## 기능
- 릴스/게시물/스토리 링크 여러 개 한 번에 붙여넣기 → 동시 다운로드(1~3개)
- 사용자명만 입력해 스토리 전체 받기
- 재인코딩 없음: 해상도 → fps → 비트레이트 순 최고 품질 그대로 저장
- 이미 받은 항목 자동 건너뛰기, 썸네일/오디오(m4a) 저장 옵션
- 다운로드 기록, 폴더 열기(PC), 파일명 형식 선택, 다크 모드
- sessionid 또는 브라우저 쿠키(PC)로 스토리·로그인 필요 콘텐츠 지원

## 빌드 (가장 쉬운 방법: GitHub Actions)
1. 이 폴더를 GitHub 저장소에 push
2. Actions 탭 → build → Run workflow
3. 완료 후 Artifacts에서 `IGSaver-exe`, `IGSaver-apk` 다운로드
(매주 자동으로 최신 yt-dlp로 재빌드됩니다 — 인스타 변경 대응)

## 로컬 빌드
- Windows EXE: `pip install -r requirements.txt` → `flet pack main.py --name IGSaver --hidden-import yt_dlp`
- Android APK: `pip install "flet[all]==0.28.3"` → `flet build apk` (JDK 17 필요)

## 주의
- Android: 설정 > 앱 > IGSaver > "모든 파일 접근" 허용해야 Download 폴더에 저장됩니다.
- Android는 ffmpeg가 없어 영상+음성 병합이 안 되고 단일 스트림 최고 화질로 받습니다.
- 본인 콘텐츠 또는 저작권자의 허락을 받은 콘텐츠에만 사용하세요 (인스타그램 이용약관/저작권).
- 인스타 계정 sessionid는 비밀번호와 같습니다. 타인에게 공유하지 마세요.
