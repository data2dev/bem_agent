# bem_agent MCP server with EnergyPlus, for Cloud Run (or any container host).
#
#   docker build -t bem-mcp .
#   docker run -p 8080:8080 -e BEM_MCP_KEYS='{"me": "<24+ char key>"}' bem-mcp
#
# Pass a GitHub token as a build secret to avoid GitHub's anonymous API rate limit:
#   docker build --secret id=gh_token,env=GH_TOKEN -t bem-mcp .

# ---- stage 1: download EnergyPlus and trim what the server never uses ----
FROM python:3.12-slim-bookworm AS energyplus
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates libgomp1 libx11-6 \
 && rm -rf /var/lib/apt/lists/*
ARG ENERGYPLUS_TAG=v25.1.0
COPY scripts/install_energyplus.sh /tmp/install_energyplus.sh
RUN --mount=type=secret,id=gh_token,required=false \
    GITHUB_TOKEN="$(cat /run/secrets/gh_token 2>/dev/null || true)" \
    ENERGYPLUS_TAG="$ENERGYPLUS_TAG" bash /tmp/install_energyplus.sh /opt/energyplus \
 && cd /opt/energyplus \
 && rm -rf Documentation MacroDataSets PreProcess/IDFVersionUpdater \
 && find ExampleFiles -type f ! -name 1ZoneUncontrolled.idf -delete \
 && find WeatherData -type f \
      ! -name 'USA_IL_Chicago-OHare.Intl.AP.725300_TMY3.*' \
      ! -name 'USA_CA_San.Francisco.Intl.AP.724940_TMY3.*' \
      ! -name 'USA_CO_Golden-NREL.724666_TMY3.*' \
      ! -name 'USA_FL_Tampa.Intl.AP.722110_TMY3.*' \
      ! -name 'USA_VA_Sterling-Washington.Dulles.Intl.AP.724030_TMY3.*' \
      -delete

# ---- stage 2: runtime ----
FROM python:3.12-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 libx11-6 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=energyplus /opt/energyplus /opt/energyplus
ENV ENERGYPLUS_DIR=/opt/energyplus \
    PATH=/opt/energyplus:$PATH \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . && energyplus --version

# Unprivileged user; all writes go to /tmp (local disk on Cloud Run) or the bucket mount.
RUN useradd --create-home --uid 10001 bem
USER bem
ENV HOST=0.0.0.0 \
    PORT=8080 \
    BEM_RUNS_DIR=/tmp/bem/runs \
    MPLCONFIGDIR=/tmp/bem/matplotlib
EXPOSE 8080
CMD ["bem-mcp", "--http"]
