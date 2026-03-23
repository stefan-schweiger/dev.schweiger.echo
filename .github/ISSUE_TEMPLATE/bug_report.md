  name: Bug Report
  description: Report a problem with the Echo app. Please complete the troubleshooting checklist first.
  labels: ["bug"]
  body:
    - type: markdown
      attributes:
        value: |
          ## Before you open an issue

          Most problems can be resolved with the troubleshooting steps below.
          Please work through them **before** submitting this form.

          ### Common Troubleshooting Steps

          1. **Enable 2FA with an authenticator app**
             Amazon requires 2-step verification using an authenticator app (e.g. Google Authenticator, Microsoft Authenticator).
             **SMS or email-based 2FA will NOT work** — you will see an Amazon error page with sad puppies 🐶 if this is the issue.

          2. **Disconnect → Reset → Restart**
             Go to the Echo app settings in Homey → press **Disconnect** → press **Reset** → **Restart the app**.
             This clears stored authentication and allows a fresh login.

          3. **Verify you selected the correct Amazon website**
             Make sure the Amazon region matches your account (e.g. amazon.de for Germany, amazon.co.uk for UK, amazon.com for US).

          4. **Uninstall conflicting Alexa apps**
             If you have another Alexa Homey app installed (e.g. the "Alexa" app), disable or uninstall it — they interfere with each other's authentication.

          5. **Check your DNS settings**
             Some routers (e.g. eero) hijack DNS and break the login. Try switching to Google DNS (`8.8.8.8`) or Cloudflare DNS (`1.1.1.1`).

          6. **Update the app**
             Make sure you are running the latest version of the Echo app.

          > **Note:** Connection drops every few months are expected due to how Amazon's unofficial authentication works. Usually a disconnect/reset/restart cycle fixes it.

    - type: checkboxes
      id: troubleshooting
      attributes:
        label: Troubleshooting Checklist
        description: Please confirm you have tried the steps above.
        options:
          - label: I have 2FA enabled using an **authenticator app** (not SMS/email)
            required: true
          - label: I have tried Disconnect → Reset → Restart in the app settings
            required: true
          - label: I have verified the correct Amazon website/region is selected
            required: true
          - label: I do not have another Alexa app installed on Homey
            required: true
          - label: I am running the latest version of the Echo app
            required: true

    - type: textarea
      id: description
      attributes:
        label: Description
        description: What happened? What did you expect to happen?
        placeholder: Describe the issue...
      validations:
        required: true

    - type: textarea
      id: error-message
      attributes:
        label: Error Message
        description: |
          If you see an error, paste it here.
          **Tip:** You can use the app's error flow trigger card (WHEN → Echo error) connected to a notification to capture error details.
        render: text

    - type: input
      id: echo-device
      attributes:
        label: Echo Device Model
        description: Which Echo device(s) are affected?
        placeholder: e.g. Echo Dot 5th Gen

    - type: input
      id: amazon-region
      attributes:
        label: Amazon Region
        description: Which Amazon website are you using?
        placeholder: e.g. amazon.de

    - type: input
      id: app-version
      attributes:
        label: App Version
        placeholder: e.g. 1.1.3

    - type: textarea
      id: diagnostic-id
      attributes:
        label: Diagnostic Report ID
        description: |
          Please submit a diagnostic report from Homey and paste the ID here.
          This helps with remote debugging.
        placeholder: e.g. abc12345-...

    - type: textarea
      id: additional
      attributes:
        label: Additional Context
        description: Anything else that might be helpful (screenshots, flow setup, etc.)
