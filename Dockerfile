# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

FROM mcr.microsoft.com/dotnet/sdk:10.0.302@sha256:ed034a8bf0b24ded0cbbac07e17825d8e9ebfe21e308191d0f7421eaf5ad4664 AS dotnet-build
WORKDIR /src

COPY global.json Directory.Build.props Directory.Packages.props ./
COPY src/TraceHelix.Domain/TraceHelix.Domain.csproj src/TraceHelix.Domain/packages.lock.json src/TraceHelix.Domain/
COPY src/TraceHelix.Application/TraceHelix.Application.csproj src/TraceHelix.Application/packages.lock.json src/TraceHelix.Application/
COPY src/TraceHelix.Infrastructure/TraceHelix.Infrastructure.csproj src/TraceHelix.Infrastructure/packages.lock.json src/TraceHelix.Infrastructure/
COPY src/TraceHelix.Api/TraceHelix.Api.csproj src/TraceHelix.Api/packages.lock.json src/TraceHelix.Api/
COPY src/TraceHelix.Cli/TraceHelix.Cli.csproj src/TraceHelix.Cli/packages.lock.json src/TraceHelix.Cli/
RUN dotnet restore src/TraceHelix.Api/TraceHelix.Api.csproj --locked-mode \
    && dotnet restore src/TraceHelix.Cli/TraceHelix.Cli.csproj --locked-mode

COPY src/ ./src/
RUN dotnet publish src/TraceHelix.Api/TraceHelix.Api.csproj -c Release --no-restore -o /out/api \
    && dotnet publish src/TraceHelix.Cli/TraceHelix.Cli.csproj -c Release --no-restore -o /out/cli

FROM mcr.microsoft.com/dotnet/aspnet:10.0.10@sha256:1fa23fc4872d95fd71c2833ebe65d7e84a43b2d51a31d119516852f13d9505a7 AS api
WORKDIR /app/api
COPY --from=dotnet-build --chown=app:app /out/api/ ./
COPY --from=dotnet-build --chown=app:app /out/cli/ /app/cli/
RUN install -d -o app -g app /data
USER app
ENV ASPNETCORE_ENVIRONMENT=Production \
    URLS=http://127.0.0.1:5080 \
    TRACEHELIX_DB=/data/tracehelix.db
EXPOSE 5080
VOLUME ["/data"]
ENTRYPOINT ["dotnet", "TraceHelix.Api.dll"]

FROM node:24-alpine@sha256:a0b9bf06e4e6193cf7a0f58816cc935ff8c2a908f81e6f1a95432d679c54fbfd AS web-build
WORKDIR /src/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM nginxinc/nginx-unprivileged:stable-alpine@sha256:dcea25a6593307a74b09e59a47f8695c4d56943750e45add532ae0bf8b24bfd6 AS web
COPY --chown=nginx:nginx --chmod=0444 deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=web-build --chown=nginx:nginx /src/web/dist/ /usr/share/nginx/html/
EXPOSE 8080
