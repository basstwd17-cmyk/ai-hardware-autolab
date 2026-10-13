# AI Hardware Autolab — 프로젝트 개발 에이전트 플랫폼

자기 Git 프로젝트를 연결하고 시나리오를 입력하면 Lead → Builder → Verifier가 조사·설계·구현·검증을 진행합니다. 결과는 프로젝트별 지식맵에 적재합니다.

설치와 실행은 [저장소 루트 README](../README.md)를 참고하세요. 플랫폼 패키지는 ai_hardware_autolab이며, 설치 폴더 이름과 개발 프로젝트 이름은 각자 정할 수 있습니다.

- [프로젝트 연결과 개발](docs/project-development.md)
- [모델 선택](docs/model-selection.md)
- [구현 상태](STATUS.md)

기본 실행은 일반 개발이며 별도 보드·툴체인·Claude 설정이 필요하지 않습니다. 신규 작업은 실행 계약이나 Campaign을 생성하지 않습니다. 구현 변경은 격리 작업공간에 남고 사람이 검토해 원본에 반영합니다.

현재 지식맵은 전체 본문, 코드, 구현·검증 파일, 참고 문서, 저장된 결과와 원본 기록을 표시합니다. 에이전트의 미검증 메모는 확인된 근거와 구분해서 재사용합니다.

runs, workspaces, webui/state, SQLite 파일은 각 설치의 운영 데이터로 Git에서 제외합니다.

이 공유판에는 보드 드라이버, 계약·Campaign 실행기와 과거 연구실 자료가 포함되지 않습니다.
