import sys
import os

from distutils.version import StrictVersion
from flask import Flask
from werkzeug.contrib.fixers import ProxyFix
from jinja2 import FileSystemLoader
from jinja2.sandbox import SandboxedEnvironment
from six.moves import input

from CTFd import utils
from CTFd.utils.migrations import migrations, migrate, upgrade, stamp, create_database
from CTFd.utils.sessions import CachingSessionInterface
from CTFd.utils.updates import update_check
from CTFd.utils.initialization import init_request_processors, init_template_filters, init_template_globals
from CTFd.utils.events import socketio
from CTFd.plugins import init_plugins

# Hack to support Unicode in Python 2 properly
# Judge python version
if sys.version_info[0] < 3:
    reload(sys)
    sys.setdefaultencoding("utf-8")

__version__ = '2.0.0'


class CTFdFlask(Flask):
    def __init__(self, *args, **kwargs):
        """Overriden Jinja constructor setting a custom jinja_environment"""
        # 生成沙盒代码,覆盖告诉运行时可以安全访问哪些属性或函数,如果模板尝试访问不安全的代码，则会引发SecurityError.
        self.jinja_environment = SandboxedBaseEnvironment
        self.jinja_environment.cache = None
        self.session_interface = CachingSessionInterface(key_prefix='session')
        Flask.__init__(self, *args, **kwargs)

    def create_jinja_environment(self):
        """Overridden jinja environment constructor"""
        return super(CTFdFlask, self).create_jinja_environment()


class SandboxedBaseEnvironment(SandboxedEnvironment):
    """SandboxEnvironment that mimics the Flask BaseEnvironment"""
    def __init__(self, app, **options):
        if 'loader' not in options:
            options['loader'] = app.create_global_jinja_loader()
        SandboxedEnvironment.__init__(self, **options)
        self.app = app


class ThemeLoader(FileSystemLoader):
    """Custom FileSystemLoader that switches themes based on the configuration value"""
    def __init__(self, searchpath, encoding='utf-8', followlinks=False):
        super(ThemeLoader, self).__init__(searchpath, encoding, followlinks)
        self.overriden_templates = {}

    def get_source(self, environment, template):
        # Check if the template has been overriden
        if template in self.overriden_templates:
            return self.overriden_templates[template], template, True

        # Check if the template requested is for the admin panel
        if template.startswith('admin/'):
            template = template[6:]  # Strip out admin/
            template = "/".join(['admin', 'templates', template])
            return super(ThemeLoader, self).get_source(environment, template)

        # Load regular theme data
        theme = utils.get_config('ctf_theme')
        template = "/".join([theme, 'templates', template])
        return super(ThemeLoader, self).get_source(environment, template)


def confirm_upgrade():
    if sys.stdin.isatty():
        print("/*\\ CTFd has updated and must update the database! /*\\")
        print("/*\\ Please backup your database before proceeding! /*\\")
        print("/*\\ CTFd maintainers are not responsible for any data loss! /*\\")
        if input('Run database migrations (Y/N)').lower().strip() == 'y':
            return True
        else:
            print('/*\\ Ignored database migrations... /*\\')
            return False
    else:
        return True


def run_upgrade():
    upgrade()
    utils.set_config('ctf_version', __version__)


def create_app(config='CTFd.config.Config'):
    app = CTFdFlask(__name__)
    with app.app_context():
        app.config.from_object(config)

        theme_loader = ThemeLoader(os.path.join(app.root_path, 'themes'), followlinks=True)
        app.jinja_loader = theme_loader

        from CTFd.models import db, Teams, Solves, Challenges, Fails, Flags, Tags, Files, Tracking

        url = create_database()

        # This allows any changes to the SQLALCHEMY_DATABASE_URI to get pushed back in
        # This is mostly so we can force MySQL's charset
        app.config['SQLALCHEMY_DATABASE_URI'] = str(url)

        # Register database
        db.init_app(app)

        # Register Flask-Migrate
        migrations.init_app(app, db)

        # Alembic sqlite support is lacking so we should just create_all anyway
        if url.drivername.startswith('sqlite'):
            db.create_all()
            stamp()
        else:
            # This creates tables instead of db.create_all()
            # Allows migrations to happen properly
            upgrade()

        from CTFd.models import ma

        ma.init_app(app)

        app.db = db
        app.VERSION = __version__

        from CTFd.cache import cache

        cache.init_app(app)
        app.cache = cache

        # If you have multiple workers you must have a shared cache
        socketio.init_app(
            app,
            async_mode=app.config.get('SOCKETIO_ASYNC_MODE'),
            message_queue=app.config.get('CACHE_REDIS_URL')
        )

        if app.config.get('REVERSE_PROXY'):
            app.wsgi_app = ProxyFix(app.wsgi_app)

        version = utils.get_config('ctf_version')

        # Upgrading from an older version of CTFd
        if version and (StrictVersion(version) < StrictVersion(__version__)):
            if confirm_upgrade():
                run_upgrade()
            else:
                exit()

        if not version:
            utils.set_config('ctf_version', __version__)

        if not utils.get_config('ctf_theme'):
            utils.set_config('ctf_theme', 'core')

        update_check(force=True)

        init_request_processors(app)
        init_template_filters(app)
        init_template_globals(app)

        # Importing here allows tests to use sensible names (e.g. api instead of api_bp)
        from CTFd.views import views
        from CTFd.teams import teams
        from CTFd.users import users
        from CTFd.challenges import challenges
        from CTFd.scoreboard import scoreboard
        from CTFd.auth import auth
        from CTFd.admin import admin
        from CTFd.api import api
        from CTFd.events import events
        from CTFd.errors import page_not_found, forbidden, general_error, gateway_error

        app.register_blueprint(views)
        app.register_blueprint(teams)
        app.register_blueprint(users)
        app.register_blueprint(challenges)
        app.register_blueprint(scoreboard)
        app.register_blueprint(auth)
        app.register_blueprint(api)
        app.register_blueprint(events)

        app.register_blueprint(admin)

        app.register_error_handler(404, page_not_found)
        app.register_error_handler(403, forbidden)
        app.register_error_handler(500, general_error)
        app.register_error_handler(502, gateway_error)

        init_plugins(app)

        return app
