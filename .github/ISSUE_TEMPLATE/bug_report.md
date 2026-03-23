---
name: Bug report
about: Create a report to help us improve
title: ''
labels: ''
assignees: ''

---

## Before you open an issue

Most problems can be resolved with the troubleshooting steps below.
Please work through them **before** submitting this form.

### Common Troubleshooting Steps

1. **Enable 2FA with an authenticator app**
    Amazon requires 2-step verification using an authenticator app (e.g. Google Authenticator, Microsoft Authenticator).
    **SMS or email-based 2FA will NOT work.**

2. **Disconnect → Reset → Restart**
    Go to the Echo app settings in Homey → press **Disconnect** → press **Reset** → **Restart the app**.
    This clears stored authentication and allows a fresh login.

3. **Verify you selected the correct Amazon website**
    Make sure the Amazon region matches your account (e.g. amazon.de for Germany, amazon.co.uk for UK, amazon.com for US).

4. **Uninstall conflicting Alexa apps**
    If you have another Alexa Homey app installed (e.g. the "Alexa" app), disable or uninstall it — they can interfere with each other's authentication.

5. **Check your DNS settings**
    Some routers (e.g. eero) hijack DNS and break the login. Try switching to Google DNS (`8.8.8.8`) or Cloudflare DNS (`1.1.1.1`).

6. **Update the app**
    Make sure you are running the latest version of the Echo app.

> **Note:** Connection drops every few months are expected due to how Amazon's unofficial authentication works. Usually a disconnect/reset/restart cycle fixes it.
