# ISL Annotation Web Tool

This repository contains a web-based annotation tool for Indian Sign Language (ISL) videos.

The tool allows annotators to:
- View a reference video for each word
- Review multiple collected user videos for the same word
- Annotate each collected video as Correct or Wrong
- Track annotation progress per annotator
- Export annotations in CSV format

This repository contains only application code.  
Videos and datasets are stored locally and are NOT included.

---

## Project Structure
isl_web/
├── app_1.py # FastAPI backend
├── index.html # Frontend UI
├── mapping.csv # Mapping between reference & collected videos
├── README.md
└── .gitignore


Generated at runtime (not committed to git):

annotations.db
previews/
frames_cache/

---

## Prerequisites

Make sure the following are installed:

- Python 3.9 or higher
- ffmpeg (with ffprobe)

Check installation:

```bash
python3 --version
ffmpeg -version

##Step 1: Clone the Repository
-git clone https://github.com/<your-username>/isl_web.git
-cd isl_web

###Step 2: Install Dependencies
It is recommended to use a virtual environment.
-python3 -m venv venv
-source venv/bin/activate
-pip install fastapi uvicorn

##Step 3: Prepare Local Data

This tool expects datasets to be present locally:
-A folder containing reference videos (one video per word)
-Folders containing collected user videos (user001–user005, etc.)
-A mapping.csv file that maps:
-word
-reference video path
-collected video path
-dataset user ID
-All video paths are local machine paths.

##Step 4: Run the Server
-Start the FastAPI server using Uvicorn:
-uvicorn app_1:app --reload --host 127.0.0.1 --port 8000

##Step 5: Open the Web App
-Open your browser and go to:
-http://127.0.0.1:8000

##How to Use the Tool
-Enter the Annotator Name (no password required)
-Select the Dataset User (user001, user002, etc.)
-Navigate word by word
-Mark each collected video as Correct or Wrong
-Progress is saved automatically
-CSV export is enabled only after completing all videos for a word
