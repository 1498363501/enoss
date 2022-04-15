# Copyright (c) 2022 Nemanja Vasiljevic <xvasil03@gmail.com>.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from swift.common.swob import wsgify, HTTPForbidden, HTTPBadRequest
from swift.common.utils import split_path, get_logger
from swift.common.request_helpers import get_sys_meta_prefix
from swift.proxy.controllers.base import get_container_info, get_account_info
from swift.common.wsgi import WSGIContext

import swift.common.middleware.enoss.destinations as destinations_module
import swift.common.middleware.enoss.payloads as payloads_module

from swift.common.middleware.enoss.configuration import (
    S3ConfigurationValidator, S3NotifiationConfiguration)
from swift.common.middleware.enoss.utils import (
    get_payload_handlers, get_destination_handlers, get_payload_handler_name,
    get_destination_handler_name)
import json
import os


class ENOSSMiddleware(WSGIContext):
    def __init__(self, app, conf, logger=None):
        self.app = app
        self.conf = conf
        self.logger = logger or get_logger(conf,
                                           log_route='eventnotifications')
        self.configuration_validator = S3ConfigurationValidator(
            self.conf["s3_schema"])
        dest_conf = json.loads(self.conf.get("destinations", "{}"))
        dest_handlers = get_destination_handlers([destinations_module])
        self.destination_handlers = {handler_name: dest_handler(dest_conf)
                                     for handler_name, dest_handler
                                     in dest_handlers.items()}
        payload_handlers = get_payload_handlers([payloads_module])
        self.payload_handlers = {handler_name: payload_handler(self.conf)
                                 for handler_name, payload_handler
                                 in payload_handlers.items()}
        self._load_admin_s3_conf()
        super(ENOSSMiddleware, self).__init__(app)

    def _load_admin_s3_conf(self):
        self.admin_s3_conf = None
        admin_s3_conf_path = self.conf.get("admin_s3_conf_path")
        if admin_s3_conf_path and os.path.isfile(admin_s3_conf_path):
            try:
                with open(admin_s3_conf_path) as f:
                    admin_s3_conf = f.read()
                    if self.configuration_validator.validate(
                        self.destination_handlers, self.payload_handlers,
                            admin_s3_conf):
                        self.admin_s3_conf = admin_s3_conf
                    else:
                        self.logger.error("Invalid s3 admin notification conf")
            except Exception as e:
                self.logger.error("error during loading admin s3 conf:{}".
                                  format(e))

    def get_notification_configuration(self, info_method, environ):
        info = info_method(environ, self.app)
        notifications_conf = info.get("sysmeta", {}).get("notifications")
        return notifications_conf

    def get_current_level(self, account, container, object):
        if object:
            return "object"
        elif container:
            return "container"
        elif account:
            return "account"
        else:
            return None

    def _get_upper_level_confs(self, curr_level, req):
        confs = [self.admin_s3_conf] if self.admin_s3_conf else []
        if curr_level in ["object", "container"]:
            notifications_conf = self.get_notification_configuration(
                get_account_info, req.environ)
            if notifications_conf:
                confs.append(notifications_conf)
        if curr_level == "object":
            notifications_conf = self.get_notification_configuration(
                get_container_info, req.environ)
            if notifications_conf:
                confs.append(notifications_conf)
        return confs

    def send_test_notification(self, curr_level, req):
        # todo check if curr_level is not None
        info_method = get_container_info if curr_level == "container" \
            else get_account_info
        notifications_conf = self.get_notification_configuration(
            info_method, req.environ)
        if notifications_conf:
            s3_conf = S3NotifiationConfiguration(notifications_conf)
            for destination_name, destination_configurations in \
                    s3_conf.destinations_configurations.items():
                handler_name = get_destination_handler_name(destination_name)
                destination_handler = self.destination_handlers[handler_name]
                for destination_configuration in destination_configurations:
                    handler_name = get_payload_handler_name(
                        destination_configuration.payload_type)
                    payload_handler = self.payload_handlers[handler_name]
                    payload = payload_handler.create_test_payload(
                        self.app, req, destination_configuration)
                    destination_handler.send_notification(payload)

    def send_notification(self, curr_level, req):
        for notification_conf in self._get_upper_level_confs(curr_level, req):
            try:
                # in case some invalid configuration is stored
                s3_conf = S3NotifiationConfiguration(notification_conf)
            except Exception as e:
                self.logger.error("{}".format(e))
                continue
            satisfied_destinations = s3_conf.get_satisfied_destinations(
                self.app, req)
            for destination_name, destination_configurations in \
                    satisfied_destinations.items():
                handler_name = get_destination_handler_name(destination_name)
                destination_handler = self.destination_handlers[handler_name]
                for destination_configuration in destination_configurations:
                    handler_name = get_payload_handler_name(
                        destination_configuration.payload_type)
                    payload_handler = self.payload_handlers[handler_name]
                    payload = payload_handler.create_payload(
                        self.app, req, destination_configuration)
                    destination_handler.send_notification(payload)

    def _post_notification(self, curr_level, req):
        if curr_level not in ["account", "container"]:
            # deny notification set on object level
            return HTTPForbidden(request=req)

        notification_sysmeta = get_sys_meta_prefix(curr_level) \
            + 'notifications'
        # todo can contain too many white spaces in body
        # maybe transform to json then to str back to remove them
        config = req.body.decode()
        if not config:
            # body is empty => delete stored configuration
            req.headers[notification_sysmeta] = ""
        if self.configuration_validator.validate(
                self.destination_handlers, self.payload_handlers, config):
            req.headers[notification_sysmeta] = config
        else:
            return HTTPBadRequest(request=req, content_type='text/plain',
                                  body='Invalid configuration')

    def _get_notification(self, curr_level, resp):
        info_method = get_container_info if curr_level == "container" \
            else get_account_info
        conf = self.get_notification_configuration(
            info_method, resp.environ)
        resp.body = str.encode(conf if conf else '')

    @wsgify
    def __call__(self, req):
        if req.headers.get("X-Backend-EventNotification-Ignore"):
            return req.get_response(self.app)
        # swift can call it self recursively
        # => we want only one notification per user request
        req.headers["X-Backend-EventNotification-Ignore"] = True

        version, account, container, object = split_path(
            req.environ['PATH_INFO'], 1, 4, rest_with_last=True)
        curr_level = self.get_current_level(account, container, object)
        event_configation_changed = False
        if req.method == "POST" and req.query_string == "notification":
            HTTP_err = self._post_notification(curr_level, req)
            if not HTTP_err:
                event_configation_changed = True
            else:
                # forbidden or bad request
                return HTTP_err

        resp = req.get_response(self.app)

        try:
            # sending notifications can be unsuccessful and throw exceptions
            if event_configation_changed:
                self.send_test_notification(curr_level, resp)
            self.send_notification(curr_level, resp)
        except Exception as e:
            self.logger.error("error:{}".format(e))
        # todo: better way to test query_string
        if (req.method == "GET" and req.query_string
                and req.query_string.startswith("notification")
                and resp.is_success):  # todo ACL
            self._get_notification(curr_level, resp)
        return resp


def enoss_factory(global_conf, **local_conf):
    conf = global_conf.copy()
    conf.update(local_conf)

    def enoss_factory(app):
        return ENOSSMiddleware(app, conf)
    return enoss_factory
