# Privacy protections and business contracting protections

This explains what the code does. It isn't legal advice. The privacy policy, candidate terms and employer placement terms must be written or reviewed by an Australian lawyer before launch.

## Privacy protections in the code

| Protection | How it works |
|---|---|
| Candidates control who sees them | Employers see licences, skills, experience and state, but not name, email, phone, summary, employer names or documents, until the candidate approves that employer. Candidates can decline or withdraw approval. |
| Hide from employers | A "Show my profile to employers" switch removes the candidate from all searches. |
| Download their data | Every user can download a ZIP of everything held about them from their account page: profile, licences, work history, uploaded files, which employers asked for or unlocked their details, shortlists they're on (with the employer's status and notes), hires, reminders and their own activity. Admins can export any user's data to answer a request made by email. Every export is logged. Passwords, login tokens and the fee an employer was charged are left out. |
| Delete the account | Candidates can delete their account; their profile, licences, history and uploaded files are removed. Business records (introductions, hires, invoices) are kept, as they must be for tax and fee purposes. |
| Collect only what matching needs | Matching uses licences, qualifications, skills, experience and location only. It doesn't use age, gender or any other personal attribute, and ties are broken by an internal id, not by name. |
| Explain automated scores | Every score comes with its reasons (what's missing, what's expiring), shown to both the employer and the candidate. |
| Limit bulk access | Each search returns at most a capped number of candidates, and the audit log records searches. |
| Opt out of emails | Licence reminders can be turned off from the profile or from a link in every email. |
| Onshore hosting | The deployment guide requires an Australian data centre (APP 8). |

**Still needed:** a privacy policy. Under amendments to the Privacy Act, privacy policies must explain significant automated decisions from **10 December 2026**: say that candidates are scored and ranked automatically, what information is used, and that employers make the final decision. You also need a process for access and correction requests (users can download their own data, and admins can export it for requests made by email; corrections are made on the site or by an admin), and a data breach response plan (Notifiable Data Breaches scheme).

**For your lawyer:** a candidate's export includes the notes employers write on their shortlists, since those are personal information held about the candidate. Ask whether any of it should be held back under Australian Privacy Principle 12; it's one line in `python/data_export.py` to remove.

## Business contracting protections in the code

These carry out the business model agreed so far: Mining Match introduces candidates and charges the employer a placement fee. It doesn't employ workers or supply labour, and candidates never pay.

| Protection | How it works |
|---|---|
| Placement-only model | Mining Match introduces candidates and invoices employers. It never pays workers or sets their rosters, so the model stays outside labour hire licensing. Keep it that way, and confirm with your lawyer. |
| Candidates never pay | There's no billing of candidates anywhere in the code. |
| Fee terms accepted before any contact | An employer can't see a candidate's contact details until they've ticked acceptance of the current published fee terms. The acceptance is recorded with the version, the employer and the time. |
| Terms can't be changed after the fact | Each published version (fee %, minimum fee, what the % applies to, fee window, guarantee days, full text) is permanent. The database refuses edits and deletions. New terms are a new version that employers must accept again. |
| Proof of introduction | Each unlock is recorded as an introduction (candidate, employer, job, terms version, date). It's kept even if the candidate later deletes their account. This is your evidence if an employer hires someone you introduced within the fee window. |
| Fee window | The fee applies to hires starting within the window (12 months by default) after the unlock. The start date can't be earlier than the unlock. |
| Guarantee | The guarantee period (90 days by default) is part of each terms version and printed on placement invoices. Replacements under the guarantee are handled by hand. The app doesn't automate refunds or replacements. |
| Invoices | Issued on the start date, never backdated, due in 14 days by default, numbered in sequence and locked once issued. Corrections are made by voiding and reissuing. |
| Anti-fraud payment details | Bank details come only from server settings. Every invoice says the details never change by email. |
| Audit trail | Unlocks, fee-terms acceptances, hires and invoice changes are written to the audit log. |

**Still needed:** the placement terms text itself, from your lawyer (the app only stores and enforces whatever version you publish). Ask the lawyer to cover:
- what counts as a hire, including contract, casual and labour-hire engagements and hires through a related company;
- the fee window and what triggers the fee;
- the guarantee conditions, including that it applies only if the invoice is paid on time;
- payment terms, late payment and disputes;
- confidentiality of candidate details;
- limitation of liability.

Candidate terms should say the service is free, how matching works, and how their details are shared.
