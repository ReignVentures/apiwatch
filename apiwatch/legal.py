"""Terms of Service and Privacy Policy for apiwatch (the feed site, the free Action and CLI, and apiwatch Pro).

Plain HTML bodies for site.py. Every statement about data has to match what the code and the operations
actually do (docs/PRO.md); change this file when they change, and bump UPDATED.
"""
from __future__ import annotations

UPDATED = "October 8, 2026"
EMAIL = "hello@reignventures.co"

TERMS = f"""<article class="legal">
<p class="crumb"><a href="index.html">apiwatch</a> / Terms</p>
<h1>Terms of Service</h1>
<p class="meta">Last updated {UPDATED}</p>

<p>These terms cover apiwatch: the public change feed at apiwatch.reignventures.co, the free apiwatch GitHub Action
and command line tool, and the paid apiwatch Pro service. apiwatch is run by Reign Ventures LLC, a Virginia limited
liability company ("we", "us"). By using apiwatch or subscribing to Pro, you agree to these terms. If you use
apiwatch for an organization, you agree on its behalf and confirm you're allowed to.</p>

<h2>What apiwatch is</h2>
<p>apiwatch tracks announced changes to third-party APIs, AI models and MCP servers, such as retirements, breaking
changes and deprecations, and points to where those changes appear in code. The feed and the free tools are offered
as they are, at no charge. The free GitHub Action and command line tool are open source under the MIT License, and
that license governs the code itself.</p>

<h2>apiwatch Pro</h2>
<ul>
<li><strong>What you get.</strong> Each night, Pro scans the default branch of the repositories you give the apiwatch
Pro GitHub App access to (except those it skips, as listed on the <a href="pro/">Pro page</a>), and opens an issue in
a repository when a tracked change that is new to that repository appears in its code.</li>
<li><strong>Access you grant.</strong> The App asks for read access to repository contents, write access to issues
(to open them), and metadata. It doesn't change code, open pull requests, or change settings. You can limit it to
selected repositories or uninstall it at any time from your GitHub settings.</li>
<li><strong>Your code stays yours.</strong> You keep all rights to your code. You allow us to read it only to provide
Pro. How we handle it is described in the <a href="privacy.html">Privacy Policy</a>.</li>
</ul>

<h2>Price, billing and cancellation</h2>
<ul>
<li>Pro costs $19 per month (or the local-currency amount shown at checkout) for one GitHub organization or user
account, plus any tax shown at checkout. It renews monthly until you cancel.</li>
<li>For Pro, Link (a Stripe service) is the reseller and merchant of record. It handles tax, receipts, payment
questions, refunds and disputes, and its terms apply to the payment itself.</li>
<li>To cancel, use your Link account at link.com (where your receipts come from), or email
<a href="mailto:{EMAIL}">{EMAIL}</a>. When you cancel by email, cancellation takes effect at the end of the current
billing period, and Pro keeps working until then. We don't give refunds for partial months, except where the law
requires it or where Link refunds a payment under its own policies.</li>
<li>Uninstalling the GitHub App stops the scans but does not cancel billing. Cancel as above.</li>
<li>If we change the price, we'll tell existing subscribers by email at least 30 days before it applies to them.</li>
</ul>

<h2>Information, not advice</h2>
<p>Each record summarizes a vendor's own announcement and links to it. Records are written and reviewed by AI agents
against that source, and their quoted evidence is re-checked against it daily, but vendors change their plans and
their pages, and we can make mistakes. Scan
results can miss code that a change affects and can flag code that it doesn't. Confirm anything that matters with
the vendor's own documentation before you act on it. apiwatch is not legal, security or professional advice.</p>

<h2>Acceptable use</h2>
<p>Don't use apiwatch to break the law, to access repositories you aren't authorized to give us access to, or to
interfere with the service. Don't scrape the website in bulk. Use the public feeds (Atom, JSON) in your own tools
and alerts instead; the records in the public repository are covered by that repository's license.</p>

<h2>Changes and ending service</h2>
<ul>
<li>We may update these terms. We'll change the date above and, for changes that affect Pro subscribers, email them
before the change applies. Continuing to use apiwatch after that means you accept the updated terms.</li>
<li>We may suspend Pro for non-payment or misuse. If we ever stop offering Pro, we'll give at least 30 days' notice
and refund any period you've paid for but won't receive.</li>
</ul>

<h2>Warranty disclaimer and limitation of liability</h2>
<p>apiwatch is provided "as is" and "as available", without warranties of any kind, whether express or implied,
including warranties of merchantability, fitness for a particular purpose, accuracy and non-infringement, to the
extent the law allows. To the extent the law allows, Reign Ventures LLC is not liable for indirect, incidental,
special, consequential or punitive damages, or for lost profits, revenue or data, arising from your use of apiwatch.
Our total liability for any claim about apiwatch is limited to the amount you paid us for Pro in the 12 months before
the claim, or $100 if you paid nothing.</p>

<h2>Governing law</h2>
<p>These terms are governed by the laws of the Commonwealth of Virginia, without regard to its conflict of law rules.
Any dispute will be handled in the state or federal courts located in Virginia.</p>

<h2>Contact</h2>
<p>Questions about these terms: <a href="mailto:{EMAIL}">{EMAIL}</a>.</p>
</article>"""


PRIVACY = f"""<article class="legal">
<p class="crumb"><a href="index.html">apiwatch</a> / Privacy</p>
<h1>Privacy Policy</h1>
<p class="meta">Last updated {UPDATED}</p>

<p>This policy explains what information apiwatch handles and why. apiwatch is run by Reign Ventures LLC, a Virginia
limited liability company. The short version: the website doesn't track you, the free tools run in your CI or on your
computer and send us nothing, and Pro reads your code only to scan it and doesn't keep it.</p>

<h2>The website</h2>
<p>apiwatch.reignventures.co uses no cookies, no analytics and no third-party scripts. It is hosted by Cloudflare,
which processes standard request information such as IP addresses, and browser network error reports, to deliver the
site and protect it from abuse.</p>

<h2>The free GitHub Action and command line tool</h2>
<p>They run in your own CI or on your own computer. The scan itself makes no network calls. The Action uses your
workflow's GitHub token only to read a pull request's changed files, post its comment and upload its report, and only
when you turn those options on. Nothing about your code or your results is sent to us.</p>

<h2>apiwatch Pro</h2>
<p><strong>When you subscribe.</strong> Checkout is handled by Link, a Stripe service, which collects your payment
details, email and billing information under its own privacy policy. Through our Stripe account we can see your name,
email, billing country or address, card brand and last four digits, payment history, the GitHub organization or user
you entered, and your subscription status. Our nightly scan uses only the GitHub account and the subscription status.
We never see your full card number.</p>
<p><strong>When the GitHub App runs.</strong> Each night the scan downloads a copy of each repository's default branch
onto a temporary machine run by GitHub Actions. It reads the code files and dependency manifests, then deletes the
copy when the scan finishes. We don't store your code. The issues the scan opens contain file paths, line numbers and
links to your code, and they live in your repository, where you control them.</p>
<p><strong>If you install without subscribing.</strong> We record only your GitHub account name, so we can follow up,
and we read none of your repositories.</p>
<p><strong>What we keep.</strong> To avoid reporting the same change twice, we keep your GitHub account name, your
repository names and numeric ids, which tracked changes were reported in each repository and when, and a short
summary of each night's run (counts, GitHub account and repository names, issue numbers, reasons a repository was
skipped, error messages, and Stripe checkout ids for orders we need to fix, but no code). This is stored in a private
GitHub repository of ours and in that repository's workflow logs.</p>

<h2>Email</h2>
<p>If you email us, we receive your address and what you write, and use them to reply. Our email runs on Google
Workspace.</p>

<h2>AI assistance</h2>
<p>We use AI tools from Anthropic to help run apiwatch. They write and review records, read the nightly run summaries
and our customer list (GitHub account names), and read and summarize the email we receive, including notices from
Stripe, so we can answer it. They don't receive your code.</p>

<h2>Who processes information for us</h2>
<ul>
<li>Stripe and its Link service: payments, tax, receipts and billing support.</li>
<li>GitHub: the GitHub App, the machines that run the nightly scan, and storage of the records we keep.</li>
<li>Cloudflare: website hosting.</li>
<li>Google Workspace: email.</li>
<li>Anthropic: AI assistance, as described above.</li>
</ul>
<p>We don't sell your information, share it for advertising, or use your code to train AI models. We and these
providers process information mainly in the United States.</p>

<h2>How long we keep it</h2>
<p>We keep Pro records until you ask us to delete them. Email us and we'll delete them within 30 days. Billing records
are kept by Stripe and Link as the law requires, and you can ask Link to delete your payment data.</p>

<h2>Your choices</h2>
<p>You can limit the GitHub App to selected repositories or uninstall it at any time. You can ask us what we hold
about you, or ask us to correct or delete it, by emailing <a href="mailto:{EMAIL}">{EMAIL}</a>.</p>

<h2>Children</h2>
<p>apiwatch is a tool for software teams. It isn't directed to children, and we don't knowingly collect information
from them.</p>

<h2>Changes</h2>
<p>If this policy changes, we'll update the date above. If a change affects Pro subscribers, we'll email them first.</p>

<h2>Contact</h2>
<p>Questions about this policy: <a href="mailto:{EMAIL}">{EMAIL}</a>.</p>
</article>"""
