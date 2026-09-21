# MakersLink-Scheduling
Project to develop a system to schecdule and book opening times at MakersLink

Rough roadmap:
1. Notification to email/slack
2. CSV-export?

## Login via MemberMatters (OpenID Connect)

Members can sign in with their MemberMatters account. MemberMatters ships an
OpenID Connect provider (django-oidc-provider, mounted at `/api/openid/`), and
this project acts as a relying party using django-allauth.

Password login keeps working alongside it, so existing accounts are not
disrupted.

### 1. Create the client in MemberMatters

In the MemberMatters admin, add an **OpenID Connect Provider Client**:

* Client type: `Confidential`
* Response type: `code (Authorization Code Flow)`
* Redirect URI: `https://<scheduling-host>/accounts/oidc/membermatters/login/callback/`

If MemberMatters has never signed a token before, generate its key first:

    python manage.py creatersakey

Note the generated Client ID and Client Secret.

### 2. Configure this application

Three environment variables register the provider. Leave any of them unset and
the provider is not registered at all, so the login page shows only the
password form.

| Variable | Description |
| --- | --- |
| `MEMBERMATTERS_SERVER_URL` | Base OIDC URL, e.g. `https://members.example.org/api/openid/`. Every endpoint is discovered from `.well-known/openid-configuration` underneath it. |
| `MEMBERMATTERS_CLIENT_ID` | Client ID from step 1 |
| `MEMBERMATTERS_CLIENT_SECRET` | Client secret from step 1 |

Two optional variables control how much MemberMatters governs the local account:

| Variable | Default | Description |
| --- | --- | --- |
| `MEMBERMATTERS_SYNC_IS_ACTIVE` | `true` | Mirror the MemberMatters membership state onto `is_active` at every login, so a lapsed membership loses access to the booking system. |
| `MEMBERMATTERS_SYNC_IS_STAFF` | `false` | Grant local staff rights to MemberMatters staff/admin. Off by default, because staff here also means edit access to calendars, templates and rules. Superusers are never demoted. |

Add them to the `docker run` command alongside the existing secrets:

    -e "MEMBERMATTERS_SERVER_URL=https://members.example.org/api/openid/" \
    -e "MEMBERMATTERS_CLIENT_ID=$CLIENT_ID" \
    -e "MEMBERMATTERS_CLIENT_SECRET=$CLIENT_SECRET"

Then apply the new tables:

    python manage.py migrate

### 3. Existing members link their account

Accounts are deliberately **not** matched on e-post address, because that would
let a provider take over any local account by asserting an address. Existing
members link their own account instead:

1. Log in with e-post and password as usual.
2. Open **Kopplade konton** in the menu.
3. Click **Koppla MemberMatters** and authenticate.

From then on either method signs them into the same account. Someone who tries
MemberMatters before linking is sent back to the login page with an explanation
rather than a signup form.

Staff can also map an account by hand in Django admin under
**Social accounts**: create a row with provider `membermatters` and the member's
MemberMatters `sub` as the UID. Note that the provider column holds
`membermatters` (the configured `provider_id`), not `openid_connect`.

### Adding another identity provider later

Nothing here is MemberMatters-specific beyond the configuration. A second
OpenID Connect provider is one more entry in the `APPS` list in
`SOCIALACCOUNT_PROVIDERS`; a non-OIDC provider (Slack, Google, GitHub) is one
extra entry in `INSTALLED_APPS` plus its own credentials. A user can hold one
linked identity per provider.

To deploy:
First on your local machine:
# Pull including current tags
git pull
# Make sure to get tags
git fetch --tags
# Look at old tags
git tag
# Invent a new version number
git tag $VERSION_NUM
# Push tag to github
git push origin $VERSION_NUM
# Build docker image locally
sudo docker build -t makerslink_scheduling .
# Save docker image
sudo docker save -o ./MakersLink-Scheduling-$VERSION_NUM.tar makerslink_scheduling:latest
# Change owner to your self
sudo chown me:me ./MakersLink-Scheduling-$VERSION_NUM.tar
# Push image to server
scp MakersLink-Scheduling-$VERSION_NUM.tar somebody@somewhere:/a/dir

Then on the server:
# Load docker image
sudo docker load -i MakersLink-Scheduling-$VERSION_NUM.tar
# Start with this command (on our server this is in the start_scheduling script).
sudo docker run -dt -e "DJANGO_SECRET=$SUPER_DUPER_SECRET" -e "SCHEDULING_PASS=$DIFFERENT_SECRET" --name scheduling --restart unless-stopped  -v /somewhere/db:/scheduling/makerslink/makerslink/db -v /somewhere/pks:/scheduling/pks -p 8000:8000 makerslink_scheduling
