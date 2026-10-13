# Internal identity entry

The internal identity is for the deploying organization's internal staff and grants full operating rights on the public instance. Anonymous
visitors neither need nor see this entry; this page only describes that it exists and what it is for.

## Location

The entry is at the very bottom of the settings page `/settings`, in the "Advanced" section, which is collapsed by default; expanding it
shows the "Identity" subsection with the note "For internal staff." The entry does not appear on the landing page, the about page, the
command palette, search, the help menu or keyboard shortcuts.

## Purpose

After verification, the role badge in the top bar changes to "Internal · full access", and for the validity of the identity (12 h) it
grants:

- operating rights on the shared show;
- a reserved sandbox slot that does not count against the public limit, a longer session lifetime, and no preemption;
- interface and API features for internal staff, such as performance reports.

By default the identity token is kept only in the current tab; it can optionally be remembered on this device for 12 h, and it can be revoked
at any time with "Sign out of the internal identity". After leaving, sandboxes created as internal revert to the public tier at their next lifecycle
check.

## Security conventions

- The passphrase is configured by the deployer on the server (written as a file during deployment, see {doc}`../topics/deployment`) and
  never appears in frontend code, the repository, logs or any document.
- The server compares the passphrase in one place only, with a constant-time comparison and a fixed delay, and rate-limits by address
  prefix; repeated failures trigger a temporary lockout.
- The input field is cleared immediately after submission, and the passphrase is never written to browser storage, logs or error reports.
- On deployments without a configured passphrase the entry reports "Internal identity is not enabled on this deployment".

Staff who need the internal identity should ask the deployer's operations lead.
