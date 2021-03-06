# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2013 OpenStack LLC
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""Keystone UUID Token Provider"""

from __future__ import absolute_import

import sys
import uuid

from keystone.common import dependency
from keystone.common import logging
from keystone import config
from keystone import exception
from keystone.openstack.common import timeutils
from keystone import token
from keystone.token import provider as token_provider
from keystone import trust


LOG = logging.getLogger(__name__)
CONF = config.CONF
DEFAULT_DOMAIN_ID = CONF.identity.default_domain_id


@dependency.requires('catalog_api', 'identity_api')
class V3TokenDataHelper(object):
    """Token data helper."""
    def __init__(self):
        if CONF.trust.enabled:
            self.trust_api = trust.Manager()

    def _get_filtered_domain(self, domain_id):
        domain_ref = self.identity_api.get_domain(domain_id)
        return {'id': domain_ref['id'], 'name': domain_ref['name']}

    def _get_filtered_project(self, project_id):
        project_ref = self.identity_api.get_project(project_id)
        filtered_project = {
            'id': project_ref['id'],
            'name': project_ref['name']}
        filtered_project['domain'] = self._get_filtered_domain(
            project_ref['domain_id'])
        return filtered_project

    def _populate_scope(self, token_data, domain_id, project_id):
        if 'domain' in token_data or 'project' in token_data:
            # scope already exist, no need to populate it again
            return

        if domain_id:
            token_data['domain'] = self._get_filtered_domain(domain_id)
        if project_id:
            token_data['project'] = self._get_filtered_project(project_id)

    def _get_roles_for_user(self, user_id, domain_id, project_id):
        roles = []
        if domain_id:
            roles = self.identity_api.get_roles_for_user_and_domain(
                user_id, domain_id)
        if project_id:
            roles = self.identity_api.get_roles_for_user_and_project(
                user_id, project_id)
        return [self.identity_api.get_role(role_id) for role_id in roles]

    def _populate_user(self, token_data, user_id, domain_id, project_id,
                       trust):
        if 'user' in token_data:
            # no need to repopulate user if it already exists
            return

        user_ref = self.identity_api.get_user(user_id)
        if CONF.trust.enabled and trust and 'OS-TRUST:trust' not in token_data:
            trustor_user_ref = (self.identity_api.get_user(
                                trust['trustor_user_id']))
            if not trustor_user_ref['enabled']:
                raise exception.Forbidden(_('Trustor is disabled.'))
            if trust['impersonation']:
                user_ref = trustor_user_ref
            token_data['OS-TRUST:trust'] = (
                {
                    'id': trust['id'],
                    'trustor_user': {'id': trust['trustor_user_id']},
                    'trustee_user': {'id': trust['trustee_user_id']},
                    'impersonation': trust['impersonation']
                })
        filtered_user = {
            'id': user_ref['id'],
            'name': user_ref['name'],
            'domain': self._get_filtered_domain(user_ref['domain_id'])}
        token_data['user'] = filtered_user

    def _populate_roles(self, token_data, user_id, domain_id, project_id,
                        trust):
        if 'roles' in token_data:
            # no need to repopulate roles
            return

        if CONF.trust.enabled and trust:
            token_user_id = trust['trustor_user_id']
            token_project_id = trust['project_id']
            #trusts do not support domains yet
            token_domain_id = None
        else:
            token_user_id = user_id
            token_project_id = project_id
            token_domain_id = domain_id

        if token_domain_id or token_project_id:
            roles = self._get_roles_for_user(token_user_id,
                                             token_domain_id,
                                             token_project_id)
            filtered_roles = []
            if CONF.trust.enabled and trust:
                for trust_role in trust['roles']:
                    match_roles = [x for x in roles
                                   if x['id'] == trust_role['id']]
                    if match_roles:
                        filtered_roles.append(match_roles[0])
                    else:
                        raise exception.Forbidden(
                            _('Trustee have no delegated roles.'))
            else:
                for role in roles:
                    filtered_roles.append({'id': role['id'],
                                           'name': role['name']})

            # user has no project or domain roles, therefore access denied
            if not filtered_roles:
                if token_project_id:
                    msg = _('User %(user_id)s have no access '
                            'to project %(project_id)s') % {
                                'user_id': user_id,
                                'project_id': token_project_id}
                else:
                    msg = _('User %(user_id)s have no access '
                            'to domain %(domain_id)s') % {
                                'user_id': user_id,
                                'domain_id': token_domain_id}
                LOG.debug(msg)
                raise exception.Unauthorized(msg)

            token_data['roles'] = filtered_roles

    def _populate_service_catalog(self, token_data, user_id,
                                  domain_id, project_id, trust):
        if 'catalog' in token_data:
            # no need to repopulate service catalog
            return

        if CONF.trust.enabled and trust:
            user_id = trust['trustor_user_id']
        if project_id or domain_id:
            try:
                service_catalog = self.catalog_api.get_v3_catalog(
                    user_id, project_id)
            # TODO(ayoung): KVS backend needs a sample implementation
            except exception.NotImplemented:
                service_catalog = {}
            # TODO(gyee): v3 service catalog is not quite completed yet
            # TODO(ayoung): Enforce Endpoints for trust
            token_data['catalog'] = service_catalog

    def _populate_token_dates(self, token_data, expires=None, trust=None):
        if not expires:
            expires = token.default_expire_time()
        if not isinstance(expires, basestring):
            expires = timeutils.isotime(expires, subsecond=True)
        token_data['expires_at'] = expires
        token_data['issued_at'] = timeutils.isotime(subsecond=True)

    def get_token_data(self, user_id, method_names, extras,
                       domain_id=None, project_id=None, expires=None,
                       trust=None, token=None):
        token_data = {'methods': method_names,
                      'extras': extras}

        # We've probably already written these to the token
        if token:
            for x in ('roles', 'user', 'catalog', 'project', 'domain'):
                if x in token:
                    token_data[x] = token[x]

        if CONF.trust.enabled and trust:
            if user_id != trust['trustee_user_id']:
                raise exception.Forbidden(_('User is not a trustee.'))

        self._populate_scope(token_data, domain_id, project_id)
        self._populate_user(token_data, user_id, domain_id, project_id, trust)
        self._populate_roles(token_data, user_id, domain_id, project_id, trust)
        self._populate_service_catalog(token_data, user_id, domain_id,
                                       project_id, trust)
        self._populate_token_dates(token_data, expires=expires, trust=trust)
        return {'token': token_data}


@dependency.requires('token_api', 'identity_api')
class Provider(token_provider.Provider):
    def __init__(self, *args, **kwargs):
        super(Provider, self).__init__(*args, **kwargs)
        if CONF.trust.enabled:
            self.trust_api = trust.Manager()
        self.v3_token_data_helper = V3TokenDataHelper()

    def get_token_version(self, token_data):
        if token_data and isinstance(token_data, dict):
            if 'access' in token_data:
                return token_provider.V2
            if 'token' in token_data and 'methods' in token_data['token']:
                return token_provider.V3
        raise token_provider.UnsupportedTokenVersionException()

    def _get_token_id(self, token_data):
        return uuid.uuid4().hex

    def _issue_v3_token(self, **kwargs):
        user_id = kwargs.get('user_id')
        method_names = kwargs.get('method_names')
        expires_at = kwargs.get('expires_at')
        project_id = kwargs.get('project_id')
        domain_id = kwargs.get('domain_id')
        auth_context = kwargs.get('auth_context')
        trust = kwargs.get('trust')
        metadata_ref = kwargs.get('metadata_ref')
        # for V2, trust is stashed in metadata_ref
        if (CONF.trust.enabled and not trust and metadata_ref and
                'trust_id' in metadata_ref):
            trust = self.trust_api.get_trust(metadata_ref['trust_id'])
        token_data = self.v3_token_data_helper.get_token_data(
            user_id,
            method_names,
            auth_context.get('extras') if auth_context else None,
            domain_id=domain_id,
            project_id=project_id,
            expires=expires_at,
            trust=trust)

        token_id = self._get_token_id(token_data)
        try:
            expiry = token_data['token']['expires_at']
            if isinstance(expiry, basestring):
                expiry = timeutils.normalize_time(
                    timeutils.parse_isotime(expiry))
            # FIXME(gyee): is there really a need to store roles in metadata?
            role_ids = []
            metadata_ref = kwargs.get('metadata_ref', {})
            if 'project' in token_data['token']:
                # project-scoped token, fill in the v2 token data
                # all we care are the role IDs
                role_ids = [r['id'] for r in token_data['token']['roles']]
                metadata_ref = {'roles': role_ids}
            if trust:
                metadata_ref.setdefault('trust_id', trust['id'])
                metadata_ref.setdefault('trustee_user_id',
                                        trust['trustee_user_id'])
            data = dict(key=token_id,
                        id=token_id,
                        expires=expiry,
                        user=token_data['token']['user'],
                        tenant=token_data['token'].get('project'),
                        metadata=metadata_ref,
                        token_data=token_data,
                        trust_id=trust['id'] if trust else None)
            self.token_api.create_token(token_id, data)
        except Exception:
            exc_info = sys.exc_info()
            # an identical token may have been created already.
            # if so, return the token_data as it is also identical
            try:
                self.token_api.get_token(token_id)
            except exception.TokenNotFound:
                raise exc_info[0], exc_info[1], exc_info[2]

        return (token_id, token_data)

    def issue_token(self, version='v3.0', **kwargs):
        if version == token_provider.V3:
            return self._issue_v3_token(**kwargs)
        raise token_provider.UnsupportedTokenVersionException

    def _verify_token(self, token_id, belongs_to=None):
        """Verify the given token and return the token_ref."""
        token_ref = self.token_api.get_token(token_id=token_id)
        assert token_ref
        if belongs_to:
            assert token_ref['tenant']['id'] == belongs_to
        return token_ref

    def revoke_token(self, token_id):
        self.token_api.delete_token(token_id=token_id)

    def _validate_v3_token(self, token_id):
        token_ref = self._verify_token(token_id)
        # FIXME(gyee): performance or correctness? Should we return the
        # cached token or reconstruct it? Obviously if we are going with
        # the cached token, any role, project, or domain name changes
        # will not be reflected. One may argue that with PKI tokens,
        # we are essentially doing cached token validation anyway.
        # Lets go with the cached token strategy. Since token
        # management layer is now pluggable, one can always provide
        # their own implementation to suit their needs.
        token_data = token_ref.get('token_data')
        if not token_data or 'token' not in token_data:
            # token ref is created by V2 API
            project_id = None
            project_ref = token_ref.get('tenant')
            if project_ref:
                project_id = project_ref['id']
            token_data = self.v3_token_data_helper.get_token_data(
                token_ref['user']['id'],
                ['password', 'token'],
                {},
                project_id=project_id,
                expires=token_ref['expires'])
        return token_data

    def validate_token(self, token_id, belongs_to=None,
                       version='v3.0'):
        try:
            if version == token_provider.V3:
                return self._validate_v3_token(token_id)
            raise token_provider.UnsupportedTokenVersionException()
        except exception.TokenNotFound as e:
            LOG.exception(_('Failed to verify token'))
            raise exception.Unauthorized(e)

    def check_token(self, token_id, belongs_to=None,
                    version='v3.0', **kwargs):
        try:
            if version == token_provider.V3:
                self._verify_token(token_id)
            else:
                raise token_provider.UnsupportedTokenVersionException()
        except exception.TokenNotFound as e:
            LOG.exception(_('Failed to verify token'))
            raise exception.Unauthorized(e)
