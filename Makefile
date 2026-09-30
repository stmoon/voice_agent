PY ?= .venv/bin/python
JUNIT := reports/junit.xml

.PHONY: test verify verify-live plan

test:
	$(PY) -m pytest -q

# 인수 테스트 → JUnit XML → 노션 체크박스 갱신 (테스트 실패해도 sync 는 돈다: 회귀 해제용)
verify:
	@mkdir -p reports
	-$(PY) -m pytest -q --junitxml=$(JUNIT)
	$(PY) tools/notion_sync.py $(JUNIT)

# 실제 claude 를 띄우는 live 테스트 포함 (로그인 필요, 토큰 소모)
verify-live:
	@mkdir -p reports
	-VC_LIVE=1 $(PY) -m pytest -q --junitxml=$(JUNIT)
	$(PY) tools/notion_sync.py $(JUNIT)

plan:
	$(PY) tools/notion_sync.py $(JUNIT) --plan-only
