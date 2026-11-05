| Scenario | Complete plan without errors | Outcome | Clarifications | To the first recommendation, s | Connection errors |
|---|---|---|---:|---:|---|
| Moscow: a complete request, hotels only | yes | ok | 0 | 17.3 | - |
| Saint Petersburg: an incomplete request | yes | ok | 1 | 11.2 | - |
| Saint Petersburg: a trip with edits | yes | ok | 0 | 7.2 | - |
| Russia: several cities | yes | ok | 0 | 25.5 | - |
| A vague request for the sea | no | directions | 0 | - | - |
| Japan: Tokyo and Osaka | no | partial | 0 | 45.9 | TRIP_DATES_MISMATCH |
| The sea: from a vague request to a plan | yes | ok | 1 | 73.3 | - |
| Bali for a week | yes | ok | 0 | 34.6 | - |
| Train only | yes | ok | 0 | 8.1 | - |
| A family with a dog in Sochi | yes | ok | 0 | 15.3 | - |
| A day trip "tomorrow" | yes | ok | 0 | 6.1 | - |
| By bus, no flights | yes | ok | 0 | 9.2 | - |
| Re-planning: four people, no breakfast | yes | ok | 0 | 9.2 | - |

Complete plans without errors: 11 of 13; 1 more stopped at choosing a destination (by design: they wait for the user to choose). Partial plans, timeouts and errors are counted separately, see the Outcome column. Clarifications on average: 0.2. Median time to the first recommendation: 11.2 s.
