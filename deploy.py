#!/usr/bin/env python3
"""Deploy Docker projects from this inventory. Python 3.12+, Docker, Git and SOPS."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

from configuration import (
    REPOSITORY, atomic_write, decrypt, env_text, host, prepare_project,
    project, projects_on_host, read_json, resolve, slug, under, write_json,
)
from source import Source, git, validate_ref, validate_repository

TICK = chr(96)


def run(*args, capture=False, check=True):
    result = subprocess.run(
        list(map(str, args)), text=True, check=check,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout.strip() if capture else result.returncode


def inspect(name):
    result = subprocess.run(
        ["docker", "inspect", name], capture_output=True, text=True,
    )
    return json.loads(result.stdout)[0] if result.returncode == 0 else None


def stop(name):
    if name and inspect(name):
        run("docker", "stop", "--time", "75", name)
        run("docker", "rm", name)


def definition(name, service):
    value = project(name)
    slug(service)
    if service not in value["services"]:
        raise ValueError(f"Unknown service {service}; available: {', '.join(value['services'])}")
    return value, value["services"][service]


def image_source(settings, service):
    seen = set()
    while settings["services"][service].get("image_from"):
        if service in seen:
            raise ValueError("Circular image_from reference")
        seen.add(service)
        service = settings["services"][service]["image_from"]
    return service, settings["services"][service]


def bind_host(root, name):
    recorded = read_json(root / "bootstrap.json")
    if recorded["host"] != name:
        raise RuntimeError("This project belongs to a different host; select the matching server")


def host_paths(root):
    return root / "host"


def api_url(host_settings, path):
    return f"http://127.0.0.1:{host_settings['admin_port']}/api/http/services{path}"


def ensure_inactive(name, host_settings):
    if inspect(name) is None:
        return
    with urllib.request.urlopen(api_url(host_settings, ""), timeout=5) as response:
        services = json.load(response)
    for item in services:
        servers = item.get("loadBalancer", {}).get("servers", [])
        if any(re.match(rf"(?:https?|h2c)://{re.escape(name)}:", server.get("url", "")) for server in servers):
            raise RuntimeError(f"{name} is referenced by Traefik; resolve the previous deployment first")


def network(settings, host_settings):
    name = settings["network"]
    slug(name)
    exists = subprocess.run(
        ["docker", "network", "inspect", name], capture_output=True,
    ).returncode == 0
    if not exists:
        run("docker", "network", "create", name)
    gateway = host_settings["gateway_container"]
    info = inspect(gateway)
    if info is None:
        raise RuntimeError("Start the shared host infrastructure first")
    connections = info["NetworkSettings"]["Networks"]
    aliases = settings.get("gateway_aliases", [])
    if name not in connections:
        args = ["docker", "network", "connect"]
        for alias in aliases:
            domain(alias)
            args += ["--alias", alias]
        run(*args, name, gateway)
    elif not set(aliases).issubset(set(connections[name].get("Aliases") or [])):
        raise RuntimeError("Gateway aliases changed; reconnect the project network during infrastructure maintenance")
    prometheus = inspect("platform-prometheus")
    if prometheus and name not in prometheus["NetworkSettings"]["Networks"]:
        run("docker", "network", "connect", name, "platform-prometheus")


def domain(value):
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value):
        raise ValueError("Invalid domain or internal DNS alias")
    return value


def port(value):
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 65535:
        raise ValueError("Ports must be integers between 1 and 65535")
    return value


def builder(root):
    result = subprocess.run(
        ["docker", "buildx", "inspect", "platform-builder"], capture_output=True,
    )
    if result.returncode:
        run(
            "docker", "buildx", "create", "--name", "platform-builder",
            "--driver", "docker-container", "--driver-opt", "memory=2g",
            "--driver-opt", "cpu-period=100000", "--driver-opt", "cpu-quota=200000",
            "--buildkitd-config", root / "control" / "buildkitd.toml",
        )


def build(root, name, service, settings, ref):
    source_name, build_settings = image_source(settings, service)
    validate_repository(build_settings["repository"])
    if shutil.disk_usage(root).free < 5 * 1024 ** 3:
        raise RuntimeError("Less than 5 GiB free; no running service was replaced")
    with Source(root, build_settings["repository"], ref) as source:
        image = f"platform/{name}-{source_name}:{source.sha}"
        context = source.path if build_settings.get("context", ".") == "." else under(source.path, build_settings["context"])
        dockerfile = under(source.path, build_settings.get("dockerfile", "Dockerfile"))
        builder(root)
        args = ["docker", "buildx", "build", "--builder", "platform-builder", "--load", "--tag", image, "--file", dockerfile]
        for key, value in build_settings.get("build_args", {}).items():
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or not isinstance(value, (str, int, bool)):
                raise ValueError("Build arguments must be public scalar values")
            args += ["--build-arg", f"{key}={value}"]
        print(f"Building {name}/{source_name}: {source.sha}", flush=True)
        run(*args, context)
        return image, source.sha


def container_args(root, name, service, settings, service_settings, container, one_off=False):
    directory = root / "projects" / name
    args = [
        "docker", "run", "--rm" if one_off else "--detach", "--name", container,
        "--network", settings["network"],
        "--stop-timeout", "75", "--log-driver", "json-file",
        "--log-opt", "max-size=10m", "--log-opt", "max-file=3",
        "--label", f"platform.project={name}", "--label", f"platform.service={service}",
    ]
    if not one_off:
        args += ["--restart", "unless-stopped"]
    group = service_settings.get("environment")
    if group:
        slug(group)
        args += ["--env-file", str(directory / "env" / f"{group}.env")]
    for volume in service_settings.get("volumes", []):
        source = under(directory, volume["source"])
        if not volume["target"].startswith("/") or ":" in volume["target"]:
            raise ValueError("Volume targets must be absolute container paths")
        source.mkdir(parents=True, exist_ok=True, mode=0o700)
        mount = f"{source}:{volume['target']}"
        if volume.get("read_only"):
            mount += ":ro"
        args += ["--volume", mount]
    if one_off:
        args += ["--no-healthcheck"]
    elif service_settings.get("health_command"):
        args += [
            "--health-cmd", service_settings["health_command"], "--health-interval", "5s",
            "--health-timeout", "5s", "--health-start-period", "30s", "--health-retries", "12",
        ]
    return args


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def wait_healthy(name, settings, service_settings):
    deadline = time.monotonic() + settings.get("health_timeout_seconds", 180)
    opener = urllib.request.build_opener(NoRedirect)
    while time.monotonic() < deadline:
        info = inspect(name)
        if info is None or info["State"]["Status"] in ("exited", "dead"):
            raise RuntimeError(f"{name} stopped before readiness; see its logs in Portainer")
        if service_settings.get("health"):
            health = service_settings["health"]
            address = info["NetworkSettings"]["Networks"][settings["network"]]["IPAddress"]
            path = health["path"]
            if not path.startswith("/") or "\n" in path:
                raise ValueError("Health paths must start with /")
            try:
                with opener.open(f"http://{address}:{port(health['port'])}{path}", timeout=4) as response:
                    if response.status == health.get("status", 200):
                        return
            except OSError:
                pass
        else:
            status = info["State"].get("Health", {}).get("Status")
            if status is None:
                raise RuntimeError("Define an HTTP health endpoint or a Docker health command")
            if status == "healthy":
                return
        time.sleep(2)
    raise RuntimeError(f"{name} readiness timeout; the previous version remains running")


def routes(name, service, definition_, container, sha):
    routers, services, middlewares, expected = {}, {}, {}, []
    for index, route in enumerate(definition_.get("routes", [])):
        identifier = f"{name}-{service}-{sha[:12]}-{container.rsplit('-', 1)[-1]}-{index}"
        hostname = domain(route["host"])
        entrypoint = route.get("entrypoint", "websecure")
        if entrypoint not in ("websecure", "internal", "grpc"):
            raise ValueError("Unsupported route entrypoint")
        rule = f"Host({TICK}{hostname}{TICK})"
        for field, matcher in (("path", "Path"), ("path_prefix", "PathPrefix")):
            if route.get(field):
                path = route[field]
                if not path.startswith("/") or TICK in path or "\n" in path:
                    raise ValueError("Invalid route path")
                if field == "path_prefix":
                    rule += f" && (Path({TICK}{path}{TICK}) || PathPrefix({TICK}{path.rstrip('/')}/{TICK}))"
                else:
                    rule += f" && {matcher}({TICK}{path}{TICK})"
        router = {"entryPoints": [entrypoint], "rule": rule, "service": identifier, "priority": route.get("priority", 100)}
        if entrypoint == "websecure":
            router["tls"] = {"certResolver": "letsencrypt"}
        if route.get("strip_prefix"):
            prefix = route["strip_prefix"]
            if not prefix.startswith("/") or "\n" in prefix:
                raise ValueError("Invalid strip prefix")
            middlewares[identifier] = {"stripPrefix": {"prefixes": [prefix]}}
            router["middlewares"] = [identifier]
        scheme = route.get("scheme", "http")
        if scheme not in ("http", "h2c"):
            raise ValueError("Routes support http or h2c")
        url = f"{scheme}://{container}:{port(route['port'])}"
        balance = {"servers": [{"url": url}]}
        health = definition_.get("health")
        checked = bool(health and route.get("health_check", True) and scheme == "http")
        if checked:
            balance["healthCheck"] = {"path": health["path"], "interval": "5s", "timeout": "3s"}
        services[identifier] = {"loadBalancer": balance}
        routers[identifier] = router
        expected.append((identifier, url, checked))
    return {"http": {"routers": routers, "services": services, "middlewares": middlewares}}, expected


def confirm(host_settings, expected, removed=False):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        complete = True
        for identifier, url, checked in expected:
            try:
                with urllib.request.urlopen(api_url(host_settings, f"/{identifier}@file"), timeout=3) as response:
                    loaded = json.load(response)
                complete &= not removed and loaded.get("status") == "enabled"
                complete &= any(server.get("url") == url for server in loaded.get("loadBalancer", {}).get("servers", []))
                if checked:
                    complete &= loaded.get("serverStatus", {}).get(url) == "UP"
            except urllib.error.HTTPError as error:
                complete &= removed and error.code == 404
            except (OSError, ValueError):
                complete = False
        if complete:
            return
        time.sleep(1)
    raise RuntimeError("Traefik did not confirm the requested routes; do not stop either version manually")


def previous_routes(value):
    result = []
    for identifier, definition_ in value.get("http", {}).get("services", {}).items():
        balance = definition_["loadBalancer"]
        result.append((identifier, balance["servers"][0]["url"], "healthCheck" in balance))
    return result


def update_target(root, project_name, service, service_settings, container):
    target = host_paths(root) / "targets" / f"{project_name}--{service}.json"
    if service_settings.get("metrics"):
        metrics = service_settings["metrics"]
        write_json(target, [{
            "targets": [f"{container}:{port(metrics['port'])}"],
            "labels": {"project": project_name, "service": service, "job": metrics.get("job", service)},
        }])
    else:
        target.unlink(missing_ok=True)


def release_one(root, name, service, settings, service_settings, host_settings, image, sha, revision):
    state_path = root / "projects" / name / "state.json"
    state = read_json(state_path) if state_path.exists() else {}
    for dependency in service_settings.get("depends_on", []):
        if dependency not in state or not inspect(state[dependency]["container"]):
            raise RuntimeError(f"Deploy dependency {dependency} first")
    old = state.get(service)
    color = "green" if old and old["color"] == "blue" else "blue"
    container = f"{name}-{service}-{color}"
    ensure_inactive(container, host_settings)
    stop(container)
    route_path = host_paths(root) / "dynamic" / f"{name}--{service}.yml"
    previous = route_path.read_text() if route_path.exists() else None
    generated, expected = routes(name, service, service_settings, container, sha)
    cleanup_candidate = True
    try:
        if service_settings.get("migration"):
            args = container_args(root, name, service, settings, service_settings, f"{container}-migration", True)
            run(*args, image, *service_settings["migration"])
        run(*container_args(root, name, service, settings, service_settings, container), image, *service_settings.get("command", []))
        wait_healthy(container, settings, service_settings)
        if service_settings.get("preflight"):
            run("docker", "exec", container, *service_settings["preflight"])
        if expected:
            cleanup_candidate = False
            write_json(route_path, generated)
            try:
                confirm(host_settings, expected)
            except BaseException:
                if previous is not None:
                    atomic_write(route_path, previous)
                    confirm(host_settings, previous_routes(json.loads(previous)))
                else:
                    route_path.unlink(missing_ok=True)
                    confirm(host_settings, expected, removed=True)
                cleanup_candidate = True
                raise
        state[service] = {
            "container": container, "color": color, "image": image,
            "sha": sha, "infrastructure_sha": revision,
        }
        write_json(state_path, state)
        cleanup_candidate = False
        update_target(root, name, service, service_settings, container)
        if service_settings.get("after_release"):
            run("docker", "exec", container, *service_settings["after_release"])
        if old:
            if expected:
                time.sleep(settings.get("drain_seconds", 15))
            stop(old["container"])
        return old["image"] if old else None
    except BaseException:
        if cleanup_candidate:
            stop(container)
        raise


def release(root, name, service, ref, revision):
    validate_ref(ref)
    settings, service_settings = definition(name, service)
    host_settings = host(settings["host"])
    bind_host(root, settings["host"])
    for related in service_settings.get("related", []):
        if settings["services"][related].get("image_from") != service:
            raise ValueError("Related services must reuse their parent's image")
    prepare_project(name, settings, root)
    network(settings, host_settings)
    image, sha = build(root, name, service, settings, ref)
    old_images = set()
    previous = release_one(root, name, service, settings, service_settings, host_settings, image, sha, revision)
    if previous:
        old_images.add(previous)
    for related in service_settings.get("related", []):
        previous = release_one(root, name, related, settings, settings["services"][related], host_settings, image, sha, revision)
        if previous:
            old_images.add(previous)
    for previous in old_images - {image}:
        run("docker", "image", "rm", previous, check=False)
    print(f"Released {name}/{service}: app={sha}, infrastructure={revision}", flush=True)


def install(root, host_name, repository):
    settings = host(host_name)
    if not repository:
        repository = git("-C", REPOSITORY, "remote", "get-url", "origin")
    validate_repository(repository)
    bootstrap = root / "bootstrap.json"
    if bootstrap.exists() and read_json(bootstrap)["host"] != host_name:
        raise RuntimeError("An installation for another host already exists here")
    for path in ("control", "repositories", "sources", "keys", "host", "projects"):
        (root / path).mkdir(parents=True, exist_ok=True)
    (root / "keys").chmod(0o700)
    for filename in ("dispatch.py", "source.py"):
        atomic_write(root / "control" / filename, (REPOSITORY / filename).read_bytes())
    atomic_write(root / "control" / "buildkitd.toml", (REPOSITORY / "modules" / "buildkitd.toml").read_bytes())
    write_json(bootstrap, {"host": host_name, "repository": repository})
    print(f"Installed bootstrap in {root}; place the server age key in {root / 'keys/age.key'} with mode 600.")


def target(name, service, ref, output):
    settings = project(name)
    if service:
        definition(name, service)
    if ref:
        validate_ref(ref)
    host_settings = host(settings["host"])
    values = {"host": settings["host"], "secrets_prefix": host_settings["ssh_secrets_prefix"], "root": host_settings["root"]}
    if output:
        with Path(output).open("a") as stream:
            for key, value in values.items():
                stream.write(f"{key}={value}\n")
    else:
        print(json.dumps(values, indent=2))


def add_project(args):
    name = slug(args.name)
    host(args.host)
    validate_repository(args.repository)
    domain(args.domain)
    port(args.port)
    if not args.health_path.startswith("/"):
        raise ValueError("The health path must start with /")
    directory = REPOSITORY / "projects" / name
    if directory.exists():
        raise ValueError("This project already exists; edit its inventory instead")
    directory.mkdir()
    write_json(directory / "project.json", {
        "host": args.host, "network": f"{name}-internal", "services": {
            "web": {
                "repository": args.repository, "dockerfile": args.dockerfile,
                "health": {"port": args.port, "path": args.health_path},
                "routes": [{"host": args.domain, "port": args.port}],
            }
        },
    })
    write_json(directory / "secrets.example.json", {"values": {}, "generate": []})
    print(f"Created projects/{name}/project.json; the service is named web.")


def render_alertmanager(root, host_name, host_settings):
    directory = host_paths(root) / "alertmanager"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    tokens = directory / "tokens"
    tokens.mkdir(exist_ok=True, mode=0o700)
    receivers, routes_ = [], []
    for name, settings in projects_on_host(host_name):
        if not settings.get("alerts"):
            continue
        encrypted = REPOSITORY / "projects" / name / "secrets.enc.json"
        if not encrypted.exists():
            if name == host_settings["alerts_project"]:
                raise RuntimeError(f"Initialize secrets for the default alert destination: {name}")
            continue
        secrets = decrypt(encrypted, root)
        values = {key: resolve(value, secrets) for key, value in settings["alerts"].items()}
        if not re.fullmatch(r"-?[0-9]+", str(values["chat_id"])) or int(values["chat_id"]) == 0:
            raise ValueError(f"{name}: Telegram chat_id must be a nonzero integer")
        if not re.fullmatch(r"[0-9]+", str(values["thread_id"])) or int(values["thread_id"]) < 1:
            raise ValueError(f"{name}: Telegram thread_id must be a positive integer")
        token = values["token"]
        if not isinstance(token, str) or "\n" in token or not token.strip():
            raise ValueError(f"{name}: invalid Telegram token")
        atomic_write(tokens / name, token, 0o600)
        receivers.append({
            "name": name,
            "telegram_configs": [{
                "bot_token_file": f"/etc/alertmanager/tokens/{name}",
                "chat_id": int(values["chat_id"]),
                "message_thread_id": int(values["thread_id"]),
                "parse_mode": "",
                "send_resolved": True,
                "message": '{{ range .Alerts }}{{ .Status }}: {{ .Annotations.summary }}\n{{ .Annotations.description }}\n{{ end }}',
            }],
        })
        routes_.append({"receiver": name, "matchers": [f'project="{name}"']})
    default = host_settings["alerts_project"]
    if default not in {item["name"] for item in receivers}:
        raise RuntimeError("The host's default alert destination has not been configured")
    write_json(directory / "alertmanager.yml", {
        "route": {
            "receiver": default, "group_by": ["project", "alertname"],
            "group_wait": "30s", "group_interval": "5m", "repeat_interval": "4h",
            "routes": routes_,
        },
        "receivers": receivers,
    }, 0o600)


def render_monitoring(root, host_name):
    directory = host_paths(root)
    monitoring = directory / "monitoring"
    atomic_write(monitoring / "host-alerts.yml", (REPOSITORY / "modules/monitoring/host-alerts.yml").read_bytes())
    (monitoring / "rules").mkdir(parents=True, exist_ok=True)
    (directory / "targets").mkdir(exist_ok=True)
    dashboards = directory / "grafana/dashboards/json"
    dashboards.mkdir(parents=True, exist_ok=True)
    for file in (REPOSITORY / "modules/monitoring/dashboards").glob("*.json"):
        atomic_write(dashboards / file.name, file.read_bytes())
    for name, settings in projects_on_host(host_name):
        source = REPOSITORY / "projects" / name / "monitoring"
        if (source / "alerts.yml").exists():
            atomic_write(monitoring / "rules" / f"{name}.yml", (source / "alerts.yml").read_bytes())
        for file in (source / "dashboards").glob("*.json"):
            atomic_write(dashboards / f"{name}--{file.name}", file.read_bytes())
    static = [
        ("node-exporter", "platform-node-exporter:9100", "/metrics"),
        ("cadvisor", "platform-cadvisor:8080", "/metrics"),
        ("traefik", "platform-traefik:8082", "/metrics"),
        ("prometheus", "platform-prometheus:9090", "/metrics"),
        ("alertmanager", "platform-alertmanager:9093", "/metrics"),
    ]
    scrapes = [{
        "job_name": job, "metrics_path": path,
        "static_configs": [{"targets": [address], "labels": {"project": "infrastructure"}}],
    } for job, address, path in static]
    scrapes.append({
        "job_name": "applications", "honor_labels": False,
        "file_sd_configs": [{"files": ["/etc/prometheus-targets/*.json"], "refresh_interval": "5s"}],
    })
    write_json(monitoring / "prometheus.yml", {
        "global": {"scrape_interval": "15s", "evaluation_interval": "15s"},
        "rule_files": ["/etc/prometheus/host-alerts.yml", "/etc/prometheus/rules/*.yml"],
        "alerting": {"alertmanagers": [{"static_configs": [{"targets": ["platform-alertmanager:9093"]}]}]},
        "scrape_configs": scrapes,
    })
    write_json(directory / "grafana/datasources/prometheus.yml", {
        "apiVersion": 1,
        "datasources": [{
            "name": "Prometheus", "uid": "prometheus", "type": "prometheus",
            "access": "proxy", "url": "http://platform-prometheus:9090",
            "isDefault": True, "editable": False,
        }],
    })
    write_json(directory / "grafana/dashboards/provider.yml", {
        "apiVersion": 1,
        "providers": [{
            "name": "Platform", "type": "file", "disableDeletion": True,
            "updateIntervalSeconds": 30, "allowUiUpdates": False,
            "options": {"path": "/etc/grafana/provisioning/dashboards/json"},
        }],
    })


def render_host(root, host_name, settings):
    directory = host_paths(root)
    secrets = decrypt(REPOSITORY / "hosts" / host_name / "secrets.enc.json", root)
    required = ("ACME_EMAIL", "GRAFANA_ADMIN_USER", "GRAFANA_ADMIN_PASSWORD", "PORTAINER_ADMIN_PASSWORD")
    for key in required:
        resolve({"secret": key}, secrets)
    if len(secrets["PORTAINER_ADMIN_PASSWORD"]) < 12:
        raise ValueError("The initial Portainer password must contain at least 12 characters")
    env_dir = directory / "env"
    env_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    grafana_domain = domain(settings["grafana_domain"])
    portainer_domain = domain(settings["portainer_domain"])
    atomic_write(env_dir / "grafana.env", env_text({
        "GF_SECURITY_ADMIN_USER": secrets["GRAFANA_ADMIN_USER"],
        "GF_SECURITY_ADMIN_PASSWORD": secrets["GRAFANA_ADMIN_PASSWORD"],
        "GF_SERVER_ROOT_URL": f"https://{grafana_domain}",
        "GF_SERVER_DOMAIN": grafana_domain,
        "GF_USERS_ALLOW_SIGN_UP": False, "GF_AUTH_ANONYMOUS_ENABLED": False,
        "GF_SECURITY_COOKIE_SECURE": True, "GF_SECURITY_COOKIE_SAMESITE": "lax",
    }), 0o600)
    auth = directory / "auth"
    auth.mkdir(exist_ok=True, mode=0o700)
    atomic_write(auth / "portainer-password.txt", secrets["PORTAINER_ADMIN_PASSWORD"], 0o600)
    (directory / "data/alertmanager").mkdir(parents=True, exist_ok=True, mode=0o700)
    routers = {
        "platform-admin": {
            "entryPoints": ["admin"], "rule": f"PathPrefix({TICK}/api{TICK})",
            "service": "api@internal",
        }
    }
    services = {}
    for name, hostname, address in (
        ("grafana", grafana_domain, "http://platform-grafana:3000"),
        ("portainer", portainer_domain, "http://platform-portainer:9000"),
    ):
        routers[f"platform-{name}"] = {
            "entryPoints": ["websecure"], "rule": f"Host({TICK}{hostname}{TICK})",
            "service": f"platform-{name}", "tls": {"certResolver": "letsencrypt"},
        }
        services[f"platform-{name}"] = {"loadBalancer": {"servers": [{"url": address}]}}
    write_json(directory / "dynamic/host.yml", {"http": {"routers": routers, "services": services}})
    atomic_write(directory / "compose.yml", (REPOSITORY / "modules/host-compose.yml").read_bytes())
    return {
        **os.environ, "HOST_ROOT": str(directory), "EDGE_NETWORK": settings["network"],
        "TRAEFIK_ADMIN_PORT": str(port(settings["admin_port"])),
        "PORTAINER_DOMAIN": portainer_domain, "ACME_EMAIL": secrets["ACME_EMAIL"],
        "PLATFORM_UID": str(os.getuid()), "PLATFORM_GID": str(os.getgid()),
    }


def infrastructure(root, name, update_host, revision):
    settings = project(name)
    host_name = settings["host"]
    host_settings = host(host_name)
    bind_host(root, host_name)
    directory, _ = prepare_project(name, settings, root)
    if not update_host and inspect(host_settings["gateway_container"]) is None:
        raise RuntimeError("Run infrastructure with update_host enabled for the first installation")
    render_alertmanager(root, host_name, host_settings)
    render_monitoring(root, host_name)
    if update_host:
        environment = render_host(root, host_name, host_settings)
        atomic_write(root / "control/buildkitd.toml", (REPOSITORY / "modules/buildkitd.toml").read_bytes())
        subprocess.run([
            "docker", "compose", "--project-name", "platform", "--env-file", "/dev/null",
            "-f", str(host_paths(root) / "compose.yml"),
            "up", "--detach", "--wait", "--wait-timeout", "180",
        ], env=environment, check=True)
    for project_name, project_settings in projects_on_host(host_name):
        network(project_settings, host_settings)
    if settings.get("compose"):
        compose = under(REPOSITORY / "projects" / name, settings["compose"])
        atomic_write(directory / "compose.yml", compose.read_bytes())
        subprocess.run([
            "docker", "compose", "--project-name", f"{name}-data", "--env-file", "/dev/null",
            "-f", str(directory / "compose.yml"), "up", "--detach", "--wait", "--wait-timeout", "180",
        ], env={**os.environ, "PROJECT_ROOT": str(directory)}, check=True)
    for container in ("platform-prometheus", "platform-alertmanager"):
        run("docker", "kill", "--signal", "HUP", container)
    write_json(directory / "infrastructure.json", {"sha": revision})
    print(f"Infrastructure applied for {name}: {revision}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--revision")
    commands = parser.add_subparsers(dest="action", required=True)
    install_ = commands.add_parser("install")
    install_.add_argument("host")
    install_.add_argument("--repository")
    target_ = commands.add_parser("target")
    target_.add_argument("project")
    target_.add_argument("--service")
    target_.add_argument("--ref")
    target_.add_argument("--output")
    release_ = commands.add_parser("release")
    release_.add_argument("project")
    release_.add_argument("service")
    release_.add_argument("ref")
    infrastructure_ = commands.add_parser("infrastructure")
    infrastructure_.add_argument("project")
    infrastructure_.add_argument("--update-host", action="store_true")
    new = commands.add_parser("new-project")
    new.add_argument("name")
    new.add_argument("--host", required=True)
    new.add_argument("--repository", required=True)
    new.add_argument("--domain", required=True)
    new.add_argument("--port", type=int, required=True)
    new.add_argument("--health-path", required=True)
    new.add_argument("--dockerfile", default="Dockerfile")
    args = parser.parse_args()
    if args.action == "target":
        target(args.project, args.service, args.ref, args.output)
    elif args.action == "new-project":
        add_project(args)
    elif args.action == "install":
        root = (args.root or Path(host(args.host)["root"])).resolve()
        install(root, args.host, args.repository)
    else:
        if not args.root or not args.revision or not re.fullmatch(r"[0-9a-f]{40}", args.revision):
            raise ValueError("Release commands require --root and an exact --revision SHA; use the installed dispatch.py")
        root = args.root.resolve()
        if args.action == "release":
            release(root, args.project, args.service, args.ref, args.revision)
        else:
            infrastructure(root, args.project, args.update_host, args.revision)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Deployment stopped: {error}") from None
