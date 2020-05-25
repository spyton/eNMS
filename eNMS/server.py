from collections import defaultdict
from datetime import timedelta
from flask import (
    abort,
    Blueprint,
    Flask,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
    session,
)
from flask_httpauth import HTTPBasicAuth
from flask_login import current_user, LoginManager, login_user, logout_user
from flask_restful import abort as rest_abort, Api, Resource
from flask_wtf.csrf import CSRFProtect
from functools import wraps
from importlib import import_module
from itertools import chain
from json import load
from os import environ
from pathlib import Path
from threading import Thread
from uuid import getnode

from eNMS import app
from eNMS.database import db
from eNMS.forms import (
    form_actions,
    form_classes,
    form_postprocessing,
    form_properties,
    form_templates,
)
from eNMS.forms.administration import init_rbac_form, LoginForm
from eNMS.models import models, property_types, relationships
from eNMS.setup import properties, rbac, themes
from traceback import format_exc


class Server(Flask):
    def __init__(self, mode=None):
        static_folder = str(app.path / "eNMS" / "static")
        super().__init__(__name__, static_folder=static_folder)
        self.update_config(mode)
        self.register_extensions()
        self.register_plugins()
        self.configure_login_manager()
        self.configure_context_processor()
        self.configure_errors()
        self.configure_authentication()
        self.configure_routes()
        self.configure_rest_api()

    @staticmethod
    def catch_exceptions_and_commit(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except LookupError as exc:
                rest_abort(404, message=str(exc))
            except Exception as exc:
                rest_abort(500, message=str(exc))
            finally:
                try:
                    db.session.commit()
                except Exception as exc:
                    db.session.rollback()
                    app.log("error", format_exc())
                    rest_abort(500, message=str(exc))

        return wrapper

    @staticmethod
    def monitor_requests(function):
        @wraps(function)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                client_address = request.environ.get(
                    "HTTP_X_FORWARDED_FOR", request.environ["REMOTE_ADDR"]
                )
                app.log(
                    "warning",
                    (
                        f"Unauthorized {request.method} request from "
                        f"'{client_address}' calling the endpoint '{request.url}'"
                    ),
                )
                return redirect(url_for("blueprint.route", page="login"))
            else:
                if (
                    not current_user.is_admin
                    and request.method == "GET"
                    and request.path not in current_user.get_requests
                ):
                    return render_template("error.html", error=403), 403
                return function(*args, **kwargs)

        return decorated_function

    def update_config(self, mode):
        mode = (mode or app.settings["app"]["config_mode"]).lower()
        self.config.update(
            {
                "DEBUG": mode != "production",
                "SECRET_KEY": environ.get("SECRET_KEY", "get-a-real-key"),
                "WTF_CSRF_TIME_LIMIT": None,
                "ERROR_404_HELP": False,
                "MAX_CONTENT_LENGTH": 20 * 1024 * 1024,
                "WTF_CSRF_ENABLED": mode != "test",
                "PERMANENT_SESSION_LIFETIME": timedelta(
                    minutes=app.settings["app"]["session_timeout_minutes"]
                ),
            }
        )

    def register_plugins(self):
        for plugin in Path(app.settings["app"]["plugin_path"]).iterdir():
            if not Path(plugin / "settings.json").exists():
                continue
            module = import_module(f"eNMS.plugins.{plugin.stem}")
            with open(plugin / "settings.json", "r") as file:
                settings = load(file)
            if not settings["active"]:
                continue
            plugin = module.Plugin(self, app, db, **settings)
            if "rbac" in settings:
                for requests in ("get_requests", "post_requests"):
                    app.rbac[requests].extend(settings["rbac"].get(requests, []))
            app.rbac["menu"]["Plugins"]["pages"].update(settings.get("pages", {}))
            init_rbac_form(app.rbac)
            app.log("info", f"Loading plugin: {settings['name']}")

    def register_extensions(self):
        self.auth = HTTPBasicAuth()
        self.csrf = CSRFProtect()
        self.csrf.init_app(self)

    def configure_login_manager(self):
        login_manager = LoginManager()
        login_manager.session_protection = "strong"
        login_manager.init_app(self)

        @login_manager.user_loader
        def user_loader(name):
            return db.fetch("user", allow_none=True, name=name)

        @login_manager.request_loader
        def request_loader(request):
            return db.fetch("user", allow_none=True, name=request.form.get("name"))

    def configure_context_processor(self):
        @self.context_processor
        def inject_properties():
            return {
                "property_types": property_types,
                "form_properties": form_properties,
                "menu": rbac["menu"],
                "names": app.property_names,
                "relations": list(set(chain.from_iterable(relationships.values()))),
                "relationships": relationships,
                "service_types": {
                    service: service_class.pretty_name
                    for service, service_class in sorted(models.items())
                    if hasattr(service_class, "pretty_name")
                },
                "settings": app.settings,
                "themes": themes,
                "table_properties": app.properties["tables"],
                "user": current_user.serialized
                if current_user.is_authenticated
                else None,
                "version": app.version,
            }

    def configure_errors(self):
        @self.errorhandler(403)
        def authorization_required(error):
            return render_template("error.html", error=403), 403

        @self.errorhandler(404)
        def not_found_error(error):
            return render_template("error.html", error=404), 404

    def configure_authentication(self):
        @self.auth.verify_password
        def verify_password(username, password):
            user = db.fetch("user", name=username, allow_none=True)
            if not user or not password:
                return False
            if app.authenticate_user(name=username, password=password):
                login_user(user)
                return True

        @self.auth.get_password
        def get_password(username):
            return getattr(db.fetch("user", name=username), "password", False)

        @self.auth.error_handler
        def unauthorized():
            return make_response(jsonify({"message": "Wrong credentials."}), 401)

    def configure_routes(self):
        blueprint = Blueprint("blueprint", __name__, template_folder="../templates")

        @blueprint.route("/")
        def site_root():
            return redirect(url_for("blueprint.route", page="login"))

        @blueprint.route("/login", methods=["GET", "POST"])
        def login():
            if request.method == "POST":
                kwargs, success = request.form.to_dict(), False
                username = kwargs["name"]
                try:
                    user = app.authenticate_user(**kwargs)
                    if user:
                        login_user(user, remember=False)
                        session.permanent = True
                        success, log = True, f"User '{username}' logged in"
                    else:
                        log = f"Authentication failed for user '{username}'"
                except Exception as exc:
                    log = f"Authentication error for user '{username}' ({exc})"
                finally:
                    app.log("info" if success else "warning", log, logger="security")
                    if success:
                        return redirect(url_for("blueprint.route", page="dashboard"))
                    else:
                        abort(403)
            if not current_user.is_authenticated:
                login_form = LoginForm(request.form)
                login_form.authentication_method.choices = [
                    (method, method.capitalize())
                    for method, active in app.settings["authentication"].items()
                    if active
                ]
                return render_template("login.html", login_form=login_form)
            return redirect(url_for("blueprint.route", page="dashboard"))

        @blueprint.route("/dashboard")
        @self.monitor_requests
        def dashboard():
            return render_template(
                "dashboard.html",
                **{"endpoint": "dashboard", "properties": properties["dashboard"]},
            )

        @blueprint.route("/logout")
        @self.monitor_requests
        def logout():
            app.log(
                "info", f"User '{current_user.name}'' logging out", logger="security"
            )
            logout_user()
            return redirect(url_for("blueprint.route", page="login"))

        @blueprint.route("/table/<table_type>")
        @self.monitor_requests
        def table(table_type):
            return render_template(
                "table.html", **{"endpoint": f"table/{table_type}", "type": table_type}
            )

        @blueprint.route("/view/<view_type>")
        @self.monitor_requests
        def view(view_type):
            return render_template(
                "visualization.html", **{"endpoint": "view", "view_type": view_type}
            )

        @blueprint.route("/workflow_builder")
        @self.monitor_requests
        def workflow_builder():
            return render_template("workflow.html", endpoint="workflow_builder")

        @blueprint.route("/form/<form_type>")
        @self.monitor_requests
        def form(form_type):
            return render_template(
                f"forms/{form_templates.get(form_type, 'base')}.html",
                **{
                    "endpoint": f"forms/{form_type}",
                    "action": form_actions.get(form_type),
                    "form": form_classes[form_type](request.form),
                    "form_type": form_type,
                },
            )

        @blueprint.route("/help/<path:path>")
        @self.monitor_requests
        def help(path):
            return render_template(f"help/{path}.html")

        @blueprint.route("/view_service_results/<int:id>")
        @self.monitor_requests
        def view_service_results(id):
            result = db.fetch("run", id=id).result().result
            return f"<pre>{app.str_dict(result)}</pre>"

        @blueprint.route("/download_file/<path:path>")
        @self.monitor_requests
        def download_file(path):
            return send_file(f"/{path}", as_attachment=True)

        @blueprint.route("/<path:_>")
        @self.monitor_requests
        def get_requests_sink(_):
            abort(404)

        @blueprint.route("/", methods=["POST"])
        @blueprint.route("/<path:page>", methods=["POST"])
        @self.monitor_requests
        def route(page):
            endpoint, *args = page.split("/")
            if f"/{endpoint}" not in app.rbac["post_requests"]:
                return jsonify({"alert": "Invalid POST request."})
            if f"/{endpoint}" not in current_user.post_requests:
                return jsonify({"alert": "Error 403 Forbidden."})
            form_type = request.form.get("form_type")
            if endpoint in app.json_endpoints:
                result = getattr(app, endpoint)(*args, **request.json)
            elif form_type:
                form = form_classes[form_type](request.form)
                if not form.validate_on_submit():
                    return jsonify({"invalid_form": True, **{"errors": form.errors}})
                result = getattr(app, endpoint)(
                    *args, **form_postprocessing(form, request.form)
                )
            else:
                result = getattr(app, endpoint)(*args, **request.form)
            try:
                db.session.commit()
                return jsonify(result)
            except Exception as exc:
                db.session.rollback()
                if app.settings["app"]["config_mode"] == "debug":
                    raise
                else:
                    result = str(exc)
                return jsonify({"alert": result})

        self.register_blueprint(blueprint)

    def configure_rest_api(self):

        api = Api(self, decorators=[self.csrf.exempt])

        class CreatePool(Resource):
            decorators = [self.auth.login_required, self.catch_exceptions_and_commit]

            def post(self):
                data = request.get_json(force=True)
                db.factory(
                    "pool",
                    **{
                        "name": data["name"],
                        "devices": [
                            db.fetch("device", name=name).id
                            for name in data.get("devices", "")
                        ],
                        "links": [
                            db.fetch("link", name=name).id
                            for name in data.get("links", "")
                        ],
                        "manually_defined": True,
                    },
                )
                db.session.commit()
                return data

        class Heartbeat(Resource):
            def get(self):
                return {
                    "name": getnode(),
                    "cluster_id": app.settings["cluster"]["id"],
                }

        class Query(Resource):
            decorators = [self.auth.login_required, self.catch_exceptions_and_commit]

            def get(self, model):
                results = db.fetch(model, all_matches=True, **request.args.to_dict())
                return [
                    result.get_properties(exclude=["positions"]) for result in results
                ]

        class GetInstance(Resource):
            decorators = [self.auth.login_required, self.catch_exceptions_and_commit]

            def get(self, model, name):
                return db.fetch(model, name=name).to_dict(
                    relation_names_only=True, exclude=["positions"]
                )

            def delete(self, model, name):
                result = db.delete(model, name=name)
                return result

        class GetConfiguration(Resource):
            decorators = [self.auth.login_required, self.catch_exceptions_and_commit]

            def get(self, name):
                return db.fetch("device", name=name).configuration

        class GetResult(Resource):
            decorators = [self.auth.login_required, self.catch_exceptions_and_commit]

            def get(self, name, runtime):
                run = db.fetch(
                    "run", service_name=name, runtime=runtime, allow_none=True
                )
                if not run:
                    raise LookupError(
                        "There are no results or on-going services "
                        "for the requested service and runtime."
                    )
                else:
                    result = run.result()
                    return {
                        "status": run.status,
                        "result": result.result if result else "No results yet.",
                    }

        class UpdateInstance(Resource):
            decorators = [self.auth.login_required, self.catch_exceptions_and_commit]

            def post(self, model):
                data, result = request.get_json(force=True), defaultdict(list)
                if not isinstance(data, list):
                    data = [data]
                for instance in data:
                    if "name" not in instance:
                        result["failure"].append((instance, "Name is missing"))
                        continue
                    try:
                        object_data = app.objectify(model, instance)
                        instance = db.factory(model, **object_data)
                        result["success"].append(instance.name)
                    except Exception:
                        result["failure"].append((instance, format_exc()))
                return result

        class Migrate(Resource):
            decorators = [self.auth.login_required, self.catch_exceptions_and_commit]

            def post(self, direction):
                kwargs = request.get_json(force=True)
                return getattr(app, f"migration_{direction}")(**kwargs)

        class RunService(Resource):
            decorators = [self.auth.login_required, self.catch_exceptions_and_commit]

            def post(self):
                data = {
                    "trigger": "REST",
                    "creator": request.authorization["username"],
                    **request.get_json(force=True),
                }
                errors, devices, pools = [], [], []
                service = db.fetch("service", name=data["name"])
                handle_asynchronously = data.get("async", False)
                for device_name in data.get("devices", ""):
                    device = db.fetch("device", name=device_name)
                    if device:
                        devices.append(device.id)
                    else:
                        errors.append(f"No device with the name '{device_name}'")
                for device_ip in data.get("ip_addresses", ""):
                    device = db.fetch("device", ip_address=device_ip)
                    if device:
                        devices.append(device.id)
                    else:
                        errors.append(f"No device with the IP address '{device_ip}'")
                for pool_name in data.get("pools", ""):
                    pool = db.fetch("pool", name=pool_name)
                    if pool:
                        pools.append(pool.id)
                    else:
                        errors.append(f"No pool with the name '{pool_name}'")
                if errors:
                    return {"errors": errors}
                if devices or pools:
                    data.update({"devices": devices, "pools": pools})
                data["runtime"] = runtime = app.get_time()
                if handle_asynchronously:
                    Thread(target=app.run, args=(service.id,), kwargs=data).start()
                    return {"errors": errors, "runtime": runtime}
                else:
                    return {**app.run(service.id, **data), "errors": errors}

        class RunTask(Resource):
            decorators = [self.auth.login_required, self.catch_exceptions_and_commit]

            def post(self):
                task_id = request.get_json()
                task = db.fetch("task", id=task_id)
                data = {
                    "trigger": "Scheduler",
                    "creator": request.authorization["username"],
                    "runtime": app.get_time(),
                    "task": task_id,
                    **task.initial_payload,
                }
                if task.devices:
                    task["devices"] = [device.id for device in task.devices]
                if task.pools:
                    task["pools"] = [pool.id for pool in task.pools]
                Thread(target=app.run, args=(task.service.id,), kwargs=data).start()

        class Topology(Resource):
            decorators = [self.auth.login_required, self.catch_exceptions_and_commit]

            def post(self, direction):
                if direction == "import":
                    return app.import_topology(
                        **{
                            "replace": request.form["replace"] == "True",
                            "file": request.files["file"],
                        }
                    )
                else:
                    app.export_topology(**request.get_json(force=True))
                    return "Topology Export successfully executed."

        class Search(Resource):
            decorators = [self.auth.login_required, self.catch_exceptions_and_commit]

            def post(self):
                rest_body = request.get_json(force=True)
                kwargs = {
                    "draw": 1,
                    "columns": [{"data": column} for column in rest_body["columns"]],
                    "order": [{"column": 0, "dir": "asc"}],
                    "start": 0,
                    "length": rest_body.get("maximum_return_records", 10),
                    "form": rest_body.get("search_criteria", {}),
                    "rest_api_request": True,
                }
                return app.filtering(rest_body["type"], **kwargs)["data"]

        class Sink(Resource):
            def get(self, **_):
                rest_abort(
                    404,
                    message=f"The requested {request.method} endpoint does not exist.",
                )

            post = put = patch = delete = get

        for endpoint in app.rest_endpoints:

            def post(_, ep=endpoint):
                getattr(app, ep)()
                return f"Endpoint {ep} successfully executed."

            api.add_resource(
                type(
                    endpoint,
                    (Resource,),
                    {
                        "decorators": [
                            self.auth.login_required,
                            self.catch_exceptions_and_commit,
                        ],
                        "post": post,
                    },
                ),
                f"/rest/{endpoint}",
            )
        api.add_resource(CreatePool, "/rest/create_pool")
        api.add_resource(Heartbeat, "/rest/is_alive")
        api.add_resource(RunService, "/rest/run_service")
        api.add_resource(RunTask, "/rest/run_task")
        api.add_resource(Query, "/rest/query/<string:model>")
        api.add_resource(UpdateInstance, "/rest/instance/<string:model>")
        api.add_resource(GetInstance, "/rest/instance/<string:model>/<string:name>")
        api.add_resource(GetConfiguration, "/rest/configuration/<string:name>")
        api.add_resource(Search, "/rest/search")
        api.add_resource(GetResult, "/rest/result/<string:name>/<string:runtime>")
        api.add_resource(Migrate, "/rest/migrate/<string:direction>")
        api.add_resource(Topology, "/rest/topology/<string:direction>")
        api.add_resource(Sink, "/rest/<path:path>")


server = Server()
