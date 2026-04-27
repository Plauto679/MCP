After putting the keys in .env
In order to properly execute the bot with the right keys, the following command needs to be run.
tools/create_keys.py

Copy and paste the results in your actual .env file


In order to run the bot, the following command needs to be run. (Activate your venv to isolate dependencies)

source "/Users/albertoalfaromendoza/Library/CloudStorage/GoogleDrive-alberto.alfaro@taiico.com/Mi unidad/Freelance/Copy Trading/MCP/.venv/bin/activate"

Finally, your server will be running at http://localhost:8000 after: 

.venv/bin/uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload                                                                                     


