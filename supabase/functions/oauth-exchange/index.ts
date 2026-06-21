// supabase/functions/oauth-exchange/index.ts
//
// Receives the Google PKCE auth code from docs/index.html, exchanges it
// for tokens (this is the one step that needs the Google client_secret,
// which is why it has to happen here instead of in the browser), looks
// up the GMB account + email, and upserts the client row in Supabase.
//
// Secrets this function needs (set these in the Supabase dashboard,
// Edge Functions -> oauth-exchange -> Secrets — never put them in code
// or in git):
//   GOOGLE_CLIENT_ID
//   GOOGLE_CLIENT_SECRET
// SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are injected automatically
// by Supabase for every Edge Function, so they don't need to be set.

// Lock this down to the actual GitHub Pages origin.
const ALLOWED_ORIGIN = "https://aagdigitalco-creator.github.io";

const corsHeaders = {
  "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
  "Access-Control-Allow-Headers": "content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }
  if (req.method !== "POST") {
    return json({ ok: false, error: "Method not allowed" }, 405);
  }

  try {
    const { name, code, code_verifier, redirect_uri } = await req.json();

    if (!name || !code || !code_verifier || !redirect_uri) {
      return json({ ok: false, error: "Missing required fields" }, 400);
    }

    const GOOGLE_CLIENT_ID = Deno.env.get("GOOGLE_CLIENT_ID")!;
    const GOOGLE_CLIENT_SECRET = Deno.env.get("GOOGLE_CLIENT_SECRET")!;
    const SUPABASE_URL = Deno.env.get("SUPABASE_URL")!;
    const SERVICE_ROLE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!;

    // 1. Exchange the auth code for tokens. This is the step that needs
    //    the client_secret, so it has to run here, not in the browser.
    const tokenRes = await fetch("https://oauth2.googleapis.com/token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        code,
        client_id: GOOGLE_CLIENT_ID,
        client_secret: GOOGLE_CLIENT_SECRET,
        redirect_uri,
        grant_type: "authorization_code",
        code_verifier,
      }),
    });
    const tokenData = await tokenRes.json();

    if (!tokenRes.ok) {
      return json(
        { ok: false, error: `Google token exchange failed: ${tokenData.error_description || tokenData.error}` },
        400,
      );
    }

    const { access_token, refresh_token, expires_in } = tokenData;

    if (!refresh_token) {
      return json(
        {
          ok: false,
          error:
            "Google didn't return a refresh token. This usually means this Google account already authorized the app before. Have the client remove access at https://myaccount.google.com/permissions and try again.",
        },
        400,
      );
    }

    // 2. Get the email of the Google account that just authorized.
    let email: string | null = null;
    try {
      const userRes = await fetch("https://www.googleapis.com/oauth2/v2/userinfo", {
        headers: { Authorization: `Bearer ${access_token}` },
      });
      if (userRes.ok) {
        const userData = await userRes.json();
        email = userData.email ?? null;
      }
    } catch {
      // Non-fatal — we can still save the client without an email.
    }

    // 3. Find the GMB account ID tied to this login.
    const acctRes = await fetch(
      "https://mybusinessaccountmanagement.googleapis.com/v1/accounts",
      { headers: { Authorization: `Bearer ${access_token}` } },
    );
    const acctData = await acctRes.json();

    if (!acctRes.ok || !acctData.accounts || acctData.accounts.length === 0) {
      return json(
        { ok: false, error: "No Google Business Profile account found for this login. Make sure the client actually manages a GMB listing with this account." },
        400,
      );
    }

    const account = acctData.accounts[0];
    const accountId = account.name.split("/")[1]; // "accounts/12345" -> "12345"
    const extraAccountsNote =
      acctData.accounts.length > 1
        ? ` Note: this login manages ${acctData.accounts.length} GMB accounts — only the first ("${account.accountName}") was saved.`
        : "";

    // 4. Save (or update) the client row in Supabase.
    const tokenExpiry = new Date(Date.now() + expires_in * 1000).toISOString();

    const upsertRes = await fetch(`${SUPABASE_URL}/rest/v1/clients?on_conflict=account_id`, {
      method: "POST",
      headers: {
        apikey: SERVICE_ROLE_KEY,
        Authorization: `Bearer ${SERVICE_ROLE_KEY}`,
        "Content-Type": "application/json",
        Prefer: "resolution=merge-duplicates,return=representation",
      },
      body: JSON.stringify([
        {
          name,
          email,
          account_id: accountId,
          access_token,
          refresh_token,
          token_expiry: tokenExpiry,
        },
      ]),
    });

    if (!upsertRes.ok) {
      const errText = await upsertRes.text();
      return json({ ok: false, error: `Saved tokens but failed to write to Supabase: ${errText}` }, 500);
    }

    return json({
      ok: true,
      message: `Saved "${name}" (${email ?? "no email"}, account ${accountId}).${extraAccountsNote}`,
    });
  } catch (err) {
    return json({ ok: false, error: `Unexpected error: ${err instanceof Error ? err.message : String(err)}` }, 500);
  }
});
