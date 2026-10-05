def tracking_numbers(resp):
    return [s["tracking_number"] for s in resp["data"]]
