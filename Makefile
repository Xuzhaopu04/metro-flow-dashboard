# 本地常用命令（Windows 上没有 make，就照右侧命令手动执行）
.PHONY: install deps up down logs ps migrate makemigrations run produce consume sim test check clean

install:
	python -m venv .venv
	@echo "Windows: .venv/Scripts/activate    macOS/Linux: source .venv/bin/activate"
	@echo "激活后执行 make deps"

deps:
	python -m pip install -r requirements.txt

up:
	docker compose up -d mysql redis

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

ps:
	docker compose ps

# 只应用已有迁移。新迁移由一个人生成后提交，其他人只 migrate
migrate:
	python manage.py migrate

makemigrations:
	python manage.py makemigrations

run:
	python manage.py runserver 8000

produce:
	python simulator/simulator.py --speed 1

consume:
	python stream/consumer.py

sim: produce

test:
	pytest

check:
	python manage.py check

clean:
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +
	rm -rf .pytest_cache
