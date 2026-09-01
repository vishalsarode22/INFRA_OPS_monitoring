# InfraBeatOps deployment

## Windows

Use the existing `packaging\build.bat` and Inno Setup flow. Test the generated
executable from `dist` before creating the installer.

## SLES/Linux

Use `packaging/systemd/infrabeatops.service` with a dedicated unprivileged
service account. Keep `.env` and `config/systems.yaml` outside source control.

Verify after deployment:

    curl http://127.0.0.1:8000/healthz
    curl http://127.0.0.1:8000/readyz

The scheduler starts after a 60-second grace period and runs every 15 minutes.
