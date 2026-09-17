// playwright-cli --session stockholm-20260917 run-code --filename <this file>
// Bounded login-page inspection only. The earlier dynamic-import login attempt
// failed before filling credentials. Global MCP key mutation is not authorized,
// so this helper deliberately does not authenticate or launch inference.
async (page) => {
  return {
    loginPageVisible: await page.getByRole('textbox', { name: 'Email', exact: true }).isVisible(),
    authenticated: false,
    inferenceSubmitted: false,
    canaryBindingBlocked: 'Hosted build uses the configured global MCP key; do not replace it.',
  };
}
