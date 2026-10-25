import os
os.chdir("/home/pusatgadaiidn/mysite")
from dotenv import load_dotenv
load_dotenv(dotenv_path="/home/pusatgadaiidn/mysite/.env")
import faqbot
from flask import Flask
test_app = Flask(__name__)
result = faqbot.init_faq_bot(test_app)
print("HASIL:", result)
