from collective.workspace.interfaces import IWorkspace
from collective.workspace.vocabs import UsersSource
from plone import api
from plone.directives import form
from plone.formwidget.autocomplete import AutocompleteFieldWidget
from z3c.form import button
from z3c.form.interfaces import WidgetActionExecutionError
from zope import schema
from zope.component import getUtility
from zope.interface import directlyProvides, Invalid
from zope.schema.interfaces import IContextSourceBinder
from zope.schema.vocabulary import SimpleVocabulary, SimpleTerm

from ploneintranet.invitations.interfaces import ITokenUtility
from ploneintranet.workspace import MessageFactory as _
from ploneintranet.workspace.utils import get_storage, send_email


visibility_vocab = SimpleVocabulary([
    SimpleTerm(value=u'secret', title=_(u'Secret')),
    SimpleTerm(value=u'private', title=_(u'Private')),
    SimpleTerm(value=u'open', title=_(u'Open')),
])

join_vocab = SimpleVocabulary([
    SimpleTerm(value=u'admin', title=_(u'Admin-Managed')),
    SimpleTerm(value=u'team', title=_(u'Team-Managed')),
    SimpleTerm(value=u'self', title=_(u'Self-Managed')),
])

particip_vocab = SimpleVocabulary([
    SimpleTerm(value=u'consumers',
               title=_(u'Consumers (Read only)')),
    SimpleTerm(value=u'producers',
               title=_(u'Producers (Can create content)')),
    SimpleTerm(value=u'publishers',
               title=_(u'Publishers (Can create and publish own content)')),
    SimpleTerm(value=u'moderators',
               title=_(u'Moderators (Full content administration rights)')),
])


def user_has_email(username):
    """ make sure, that given user has an email associated """
    user = api.user.get(username=username)
    if not user.getProperty("email"):
        msg = _(
            "This user doesn't have an email associated "
            "with their account."
        )
        raise Invalid(msg)

    return True


class IPolicyForm(form.Schema):
    """ Policy form fields, essentially radio buttons"""

    external_visibility = schema.Choice(
        title=_(u"ws_external_visibility", default="External Visibility"),
        description=_(u"ws_external_visbility_description",
                      default="Who can see this workspace and its content?"),
        source=visibility_vocab,
    )

    join_policy = schema.Choice(
        title=_(u"ws_join_policy", default="Join Policy"),
        description=_(u"ws_join_policy_description",
                      default="Who can add new members?"),
        source=join_vocab,
    )

    participant_policy = schema.Choice(
        title=_(u"ws_participant_policy", default="Participant Policy"),
        description=_(u"ws_participant_policy_description",
                      default="What permissions do members have by default?"),
        source=particip_vocab,
    )


class PolicyForm(form.SchemaForm):
    """
    handle policy form submission
    """

    schema = IPolicyForm
    ignoreContext = True

    label = u"Workspace Policies"

    @button.buttonAndHandler(u'Ok')
    def handleApply(self, action):
        data, errors = self.extractData()
        if errors:
            self.status = self.formErrorsMessage
            return

        ws = self.context
        ws.set_external_visibility(data.get("external_visibility", "private"))
        ws.join_policy = data.get("join_policy", "admin")
        ws.participant_policy = data.get("participant_policy", "consumers")

        self.updateWidgets()
        self.status = "Policy updated."

    @button.buttonAndHandler(u"Cancel")
    def handleCancel(self, action):
        """User cancelled. Redirect back to the front page.
        """

    def updateWidgets(self):
        super(PolicyForm, self).updateWidgets()
        ws = self.context
        self.widgets["external_visibility"].value = ws.external_visibility
        self.widgets["join_policy"].value = ws.join_policy
        self.widgets["participant_policy"].value = ws.participant_policy


def workspaces_provider(context):
    """
    create a vocab of all workspaces in this site
    """
    catalog = api.portal.get_tool(name="portal_catalog")
    workspaces = catalog(portal_type="ploneintranet.workspace.workspacefolder")
    current = api.content.get_uuid(context)

    terms = []
    for ws in workspaces:
        if current != ws["UID"]:
            terms.append(SimpleVocabulary.createTerm(
                ws["UID"], ws["UID"], ws["Title"]))

    return SimpleVocabulary(terms)

directlyProvides(workspaces_provider, IContextSourceBinder)


class ITransferMembershipForm(form.Schema):
    """
    the form to handle transferring membership
    """

    workspace = schema.Choice(
        title=_(u"Select workspace"),
        source=workspaces_provider,
    )

    move = schema.Bool(
        title=_(u"Move"),
        description=_(u"If checked, users will be removed from workspace"),
        required=False,
    )


class TransferMembershipForm(form.SchemaForm):
    """
    handle transfer membership form submission
    """
    schema = ITransferMembershipForm
    ignoreContext = True

    label = u"Transfer membership"

    @button.buttonAndHandler(u"Ok")
    def handleApply(self, action):
        data = self.extractData()[0]

        ws = IWorkspace(self.context)
        other_ws_id = data.get("workspace")
        other_ws = IWorkspace(api.content.get(UID=other_ws_id))
        move = data.get("move", False)
        removable = []
        for member in ws.members:
            user = api.user.get(username=member)
            if user is not None:
                user_id = user.getId()
                other_ws.add_to_team(user=user_id)
            removable.append(member)

        if move:
            func = lambda member: ws.membership_factory(
                ws,
                {"user": member}).remove_from_team()
            map(func, removable)

        self.updateWidgets()
        self.status = "Members transfered."


class IInviteForm(form.Schema):
    """
    invite form schema
    """

    form.widget(user=AutocompleteFieldWidget)
    user = schema.Choice(
        title=u'User',
        source=UsersSource,
        constraint=user_has_email,
    )

    message = schema.Text(
        title=_(u"Message"),
        default=u"",
        required=False,
        )


class InviteForm(form.SchemaForm):
    """
    handle invite form submission
    """
    schema = IInviteForm
    ignoreContext = True

    label = u"Invitations"

    @button.buttonAndHandler(u"Ok")
    def handleApply(self, action):
        data = self.extractData()[0]
        given_username = data.get("user", "").strip()
        given_message = data.get("message", "") or ""
        given_message = given_message.strip()
        if not given_username:
            return

        ws = IWorkspace(self.context)
        for name in ws.members:
            member = api.user.get(username=name)
            if member is not None:
                if member.getUserName() == given_username:
                    raise WidgetActionExecutionError(
                        'user',
                        Invalid("User is already a member of this workspace"))

        user = api.user.get(username=given_username)
        email = user.getProperty("email")

        token_util = getUtility(ITokenUtility)
        token_id, token_url = token_util.generate_new_token(
            redirect_path="resolveuid/%s" % (ws.context.UID(),))
        storage = get_storage()
        storage[token_id] = (ws.context.UID(), given_username)

        current_user = api.user.get_current()
        inviter = current_user.getProperty("fullname", None)
        if not inviter:
            inviter = current_user.getUserName()

        msg_header = "You have been invited to join %s by %s" % (
            self.context.title, inviter)

        if given_message:
            optional = "Here is the message from %s\n\n" % inviter
            optional = "%s%s\n\n" % (optional, given_message)
            given_message = optional

        msg_footer = """
The following is a unique URL tied to your email address ({email}).

Following the link will make you a member of the {workspace} workspace
automatically.

{token_url}

""".format(
            email=email,
            workspace=self.context.title,
            token_url=token_url)

        message = u"{header}\n\n{optional}{footer}".format(
            header=msg_header,
            optional=given_message,
            footer=msg_footer,
        )

        subject = 'You are invited to join "%s"' % self.context.title

        send_email(email, subject, message)
        api.portal.show_message(
            'Invitation sent to %s' % email,
            self.request,
        )