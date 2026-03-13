# Pending stuff

- [] How we define the status of a story? 
    - [] Not started  zero messages in chatSession.messages. When I open a sotry for the first time I get the defualt 4 messages but even then the chatSession.messages is empty I cannot distinguish between not started at all - Open the app get 4 initial messages didn't reply
    - [] WIP
    - [] Submiited --> improvementState.story.status ?? 
Checked distinct values in db.miniStories.distinct("improvementState.story.status")



## To run every analytics collections including test
uv run python -m src.analytics_sync --include-test

## To run view
uv run python scripts/create_views.py --view user_snapshots