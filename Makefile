# Lucky Makefile —— `make up` 是 bootstrap.sh 的薄封装（PRD FR-01）
# 直接跑 bash bootstrap.sh 等价；--check 只做环境自检不启动
.PHONY: up check

up:
	bash bootstrap.sh

check:
	bash bootstrap.sh --check
