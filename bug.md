# Smart Helmet – Bug Inventory

| ID | Component        | Env        | Trigger / Scenario                                  | Expected                                      | Actual                                         | Notes / Clues                  |
|----|------------------|-----------|-----------------------------------------------------|-----------------------------------------------|-----------------------------------------------|--------------------------------|
| B1 | persist_worker   | EC2       | Start mock sender after old trip exists            | Old trip closed, new trip created + recording | Old trip stays "recording", new trip not made | Works locally, broken on EC2  |
| B2 | websocket / API  | Local+EC2 | ...                                                 | ...                                           | ...                                           | ...                            |
