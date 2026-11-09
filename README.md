# Social Media Research Archive

A local research tool for saving, searching, annotating, and thematically categorizing previously collected TikTok and Instagram posts. Designed for qualitative research into political rhetoric, ideology, prejudice, and social discourse.

## How it works

1. **Collect posts using Apify.** Web scraping is handled by third-party Apify actors; this repository does **not** implement the scrapers.
2. **Download existing Apify results** with the scripts in `downloader/`. They retrieve the available post metadata, media, transcripts and comments, verify saved files with checksums, and organize them by post.
3. **Explore and annotate the archive** with `organizer/`, a local browser application backed by SQLite. Search posts, review captions and comments, add notes and research highlights, attach standalone screenshots, and export data to CSV.
4. **Optionally use Gemini for thematic categorization.** The application applies a researcher-defined topic taxonomy to selected source evidence. Categories can be corrected manually; manual choices take precedence over automated suggestions. AI classification is optional, requires a user-supplied Gemini API key, and may incur API charges.

## Technologies

Python (standard library), SQLite, JavaScript, HTML/CSS, Apify API, and optional Google Gemini API.

## Folder structure

- `downloader/` — reads completed Apify run results and downloads/normalizes available content.
- `organizer/` — local research interface, SQLite database, manual annotations, and optional AI categorization.

## Running locally (macOS)

Requires Python 3.9 or newer. All program files for each component should remain together in their respective directories.

1. In Terminal, run `bash /path/to/downloader/Start.command` and enter a completed Apify run ID, archive destination, and your own Apify API token. This **downloads previously scraped results only** and does not start a scraping run.
2. After downloading, run `bash /path/to/organizer/Organize.command` and select the archive folder. The application opens a local browser page served from `127.0.0.1`.
3. To enable optional AI categorization, supply your own Gemini key through the browser's Settings panel. Review and correct categories as needed.

You can use the organizer without AI categorization and without a Gemini key. The program stores its database alongside the archive data, **not** inside this source repository.

## Research and methodological notes

The categories represent **subjects discussed**, not the author's identity, beliefs, or endorsement of those subjects. A post about an ideology is not necessarily an expression of support for it. Automated assignments are suggestions, not definitive interpretations; ambiguous or incomplete evidence should remain open to human review. The underlying research dataset is not included in this repository.

## Data, security, and limitations

- **No actual scraped posts, personal annotations, databases, credentials, or media are included.** Do not commit your `Saved-Posts` folder or API tokens.
- The downloader retrieves existing Apify results; coverage depends on the scraping actor and its outputs. Missing media, transcripts, or comments cannot be inferred or reconstructed.
- The Gemini feature submits selected original post captions, transcripts and optional saved images to Google's API. Private notes, manually added comments and reference screenshots are excluded by design. The key is entered at runtime rather than saved to disk.
- Built for local research use, not as a hosted, production multi-user service. Live API behavior may differ from test cases, and third-party API schemas can change.
- When using scraped social-media content, respect applicable platform policies, privacy obligations, and research ethics.

## Development note

Developed through iterative AI-assisted coding, with the research requirements, taxonomy, and workflow directed by the researcher. This project is offered as an example of research-tool development, not as a claim of independent software-engineering expertise.
