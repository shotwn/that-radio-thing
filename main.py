import thatradiothing.server
from thatradiothing import ThatRadioThing


if __name__ == "__main__":
    TRT = ThatRadioThing()
    #TRT.web_server = thatradiothing.server.WebServer(TRT)
    TRT.run()