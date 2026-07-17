.PHONY: restore build test lint verify-e2e verify-api
restore:
	dotnet restore TraceHelix.slnx
build:
	dotnet build TraceHelix.slnx -c Release
	npm --prefix web run build
test:
	dotnet test TraceHelix.slnx -c Release
	npm --prefix web test
lint:
	dotnet format TraceHelix.slnx --verify-no-changes
	npm --prefix web run lint
verify-e2e:
	bash scripts/verify-e2e.sh
verify-api:
	bash scripts/verify-api.sh
