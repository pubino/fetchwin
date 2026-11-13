# syntax=docker/dockerfile:1.7
# Pester tests and PSScriptAnalyzer lint for the Windows-side update installer.
# The script targets Windows PowerShell 5.1; the analyzer checks 5.1 syntax compatibility,
# and the Windows-only calls (DISM, registry, scheduled tasks) are mocked in the tests.
#
# Native on amd64 and arm64: same pinned Debian base as the main image, plus the official
# PowerShell tarball for the build architecture. Everything is pinned by SHA-256.

FROM python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS fetch
ARG TARGETARCH
ARG PWSH_VERSION=7.4.22
ARG PWSH_SHA256_amd64=9de443bef974f219c98a33b0f4096aace7f30b42188f37a1b14c5da7e82c8a6a
ARG PWSH_SHA256_arm64=ef95973facf60cf735f6d8caef3383394b9e8200cbd078d6068f7e14ca6b9078
ADD --checksum=sha256:4a27904c6814a5fbe4758f8e49861f6a1994aee77b71165a5c43c0371ba6c580 \
    https://www.powershellgallery.com/api/v2/package/Pester/5.7.1 /dl/Pester.zip
ADD --checksum=sha256:b9969345f78251112fb75bdddc789be8689a343472a2e09762303aa03287afab \
    https://www.powershellgallery.com/api/v2/package/PSScriptAnalyzer/1.23.0 /dl/PSScriptAnalyzer.zip
ADD https://github.com/PowerShell/PowerShell/releases/download/v${PWSH_VERSION}/powershell-${PWSH_VERSION}-linux-x64.tar.gz /dl/pwsh-amd64.tar.gz
ADD https://github.com/PowerShell/PowerShell/releases/download/v${PWSH_VERSION}/powershell-${PWSH_VERSION}-linux-arm64.tar.gz /dl/pwsh-arm64.tar.gz
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) want="$PWSH_SHA256_amd64" ;; \
      arm64) want="$PWSH_SHA256_arm64" ;; \
      *) echo "unsupported arch $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    echo "$want  /dl/pwsh-$TARGETARCH.tar.gz" | sha256sum -c -; \
    mkdir -p /opt/pwsh && tar -xzf "/dl/pwsh-$TARGETARCH.tar.gz" -C /opt/pwsh; \
    chmod +x /opt/pwsh/pwsh; \
    python -m zipfile -e /dl/Pester.zip /opt/modules/Pester/5.7.1; \
    python -m zipfile -e /dl/PSScriptAnalyzer.zip /opt/modules/PSScriptAnalyzer/1.23.0

FROM python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 \
    POWERSHELL_TELEMETRY_OPTOUT=1 \
    PSModulePath=/opt/modules
COPY --from=fetch /opt/pwsh /opt/pwsh
COPY --from=fetch /opt/modules /opt/modules
RUN useradd --create-home tester
USER tester
WORKDIR /src
COPY src/fetchwin/windows src/fetchwin/windows
COPY tests/powershell tests/powershell
CMD ["/opt/pwsh/pwsh", "-NoProfile", "-NonInteractive", "-File", "tests/powershell/run.ps1"]
