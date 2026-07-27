#!/usr/bin/env python3
"""
One-off backfill: push the 2026-07-16..07-18 EA flood alerts into the
permanent flood_history record (area_stats.json / events_master.csv.gz).

These events were correctly diffed into ea/timeline/events.json at the time
they happened, but update_flood_stats.append_and_rebuild() was silently
crashing on every call (gzip master read without compression="gzip" — fixed
in update_flood_stats.py) so they never reached the permanent record. Sourced
directly from the live ea/timeline/events.json entries of kind "raised".

Run once via GitHub Actions (needs R2 secrets), then delete this file and
its one-off workflow.
"""

import os

# code -> (date, name, area, type)
EVENTS = [
    dict(date="2026-07-16T10:54:11", code="114WACT1T1BA00",
         name="South Cornwall coast from Lizard Point to Gribbin Head excluding the Tidal Fal Estuary",
         area="Cornwall", type="Flood Alert", source="EA"),
    dict(date="2026-07-16T10:57:41", code="114WACT1T1CA00",
         name="South Cornwall coast from Gribbin Head to Rame Head",
         area="Cornwall", type="Flood Alert", source="EA"),
    dict(date="2026-07-16T08:44:29", code="063WAT23West",
         name="Tidal Thames riverside from Putney Bridge to Teddington Weir",
         area="Greater London, Hammersmith and Fulham, Hounslow, Richmond upon Thames, Wandsworth",
         type="Flood Alert", source="EA"),
    dict(date="2026-07-16T18:41:24", code="111WATCHXH",
         name="Christchurch Harbour",
         area="Bournemouth, Christchurch and Poole", type="Flood Alert", source="EA"),
    dict(date="2026-07-17T13:10:32", code="112WATSOM1",
         name="Somerset coast at Porlock Weir",
         area="Somerset", type="Flood Alert", source="EA"),
    dict(date="2026-07-17T15:45:37", code="051WACDV4C",
         name="The Essex coast from Clacton to and including, St Peters Flat and the Colne and Blackwater estuaries",
         area="Essex", type="Flood Alert", source="EA"),
    dict(date="2026-07-17T15:48:34", code="121WAC922",
         name="Tyne and Wear coast",
         area="County Durham, Hartlepool, North Tyneside, South Tyneside, Sunderland",
         type="Flood Alert", source="EA"),
    dict(date="2026-07-17T15:48:43", code="121WAC921",
         name="Northumberland coast",
         area="Northumberland", type="Flood Alert", source="EA"),
    dict(date="2026-07-18T09:12:52", code="054WATBT3",
         name="The tidal River Waveney from Ellingham to Breydon Water",
         area="Norfolk, Suffolk", type="Flood Alert", source="EA"),
    dict(date="2026-07-18T09:23:23", code="054WATBT2",
         name="The tidal River Yare from Thorpe St Andrew to Breydon Water",
         area="Norfolk", type="Flood Alert", source="EA"),
    dict(date="2026-07-18T09:32:27", code="054WATBT1",
         name="The tidal Rivers Bure, Ant and Thurne",
         area="Norfolk", type="Flood Alert", source="EA"),
    dict(date="2026-07-18T10:09:52", code="051WACDV4D",
         name="The Essex coast from St Peters Flat to and including Shoeburyness and the Crouch and Roach estuaries",
         area="Essex, Southend-on-Sea", type="Flood Alert", source="EA"),
]


def main():
    from update_flood_stats import append_and_rebuild

    R2_ACCOUNT_ID = os.environ["R2_ACCOUNT_ID"]
    R2_ACCESS_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
    R2_SECRET_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
    R2_BUCKET = os.environ["R2_BUCKET_NAME"]

    import boto3
    r2 = boto3.client(
        "s3",
        endpoint_url=f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_KEY,
        region_name="auto",
    )

    print(f"Backfilling {len(EVENTS)} event(s)…")
    append_and_rebuild(EVENTS, r2, R2_BUCKET)
    print("Done.")


if __name__ == "__main__":
    main()
