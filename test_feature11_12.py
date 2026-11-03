"""End-to-end Feature 11/12 tests using a disposable SQLite database."""
import hashlib
import http.client
import json
import os
import secrets
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

_TEMP = tempfile.TemporaryDirectory(prefix="codementor_features11_12_test_")
os.environ["DATABASE_PATH"] = os.path.join(_TEMP.name, "test.db")

import database
import server as feature11_12
import server


class Feature11And12Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="codementor_case_")
        database.DB_PATH = os.path.join(self.tmp.name, "main.sqlite3")
        database.init_db()
        feature11_12.init_store()
        conn = database.get_connection()
        next_id = conn.execute("SELECT COALESCE(MAX(id),1000)+1 FROM users").fetchone()[0]
        self.users = [next_id, next_id + 1, next_id + 2]
        for offset, user_id in enumerate(self.users):
            conn.execute("INSERT INTO users(id,username,email,display_name,password_hash) VALUES(?,?,?,?,?)",
                (user_id,f"feature-test-{user_id}",f"feature-test-{user_id}@example.invalid",f"Learner {offset+1}",database.hash_password("temporary-test-password")))
        self.tokens = {}
        now = int(time.time())
        for user_id in self.users:
            token = secrets.token_urlsafe(24)
            self.tokens[user_id] = token
            conn.execute("INSERT INTO auth_sessions(token_hash,user_id,created_at,expires_at) VALUES(?,?,?,?)",
                (hashlib.sha256(token.encode()).hexdigest(),user_id,now,now+7200))
        conn.commit();conn.close()
        self.httpd = server.ThreadedHTTPServer(("127.0.0.1",0),server.CodeMentorHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever,daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown();self.httpd.server_close();self.thread.join(timeout=2)
        self.tmp.cleanup()

    def request(self,method,path,payload=None,user=None):
        body=json.dumps(payload).encode("utf-8") if payload is not None else b""
        headers={"Content-Type":"application/json"}
        if user is not None:headers["Cookie"]=f"{server.SESSION_COOKIE}={self.tokens[user]}"
        conn=http.client.HTTPConnection("127.0.0.1",self.httpd.server_port,timeout=30)
        conn.request(method,path,body=body,headers=headers)
        response=conn.getresponse();raw=response.read();status=response.status;conn.close()
        try:data=json.loads(raw.decode("utf-8"))
        except (ValueError,UnicodeError):data=raw.decode("utf-8",errors="replace")
        return status,data

    def create_interview(self,**overrides):
        payload={"language":"python","difficulty":"Easy","topic":"Arrays","duration_minutes":15,"question_count":1}
        payload.update(overrides)
        return self.request("POST","/api/interviews",payload,user=self.users[0])

    def create_competition(self,**overrides):
        now=int(time.time())
        payload={"title":"Unit test contest","description":"A controlled test competition.","language":"python","difficulty":"Easy",
            "challenge_id":"curated:python:Easy","starts_at":now+30,"ends_at":now+900,"duration_minutes":5,"max_participants":2}
        payload.update(overrides)
        return self.request("POST","/api/competitions",payload,user=self.users[0])

    def test_interview_persists_execution_evaluation_report_and_owner_isolation(self):
        user=self.users[0];other=self.users[1]
        status,catalog=self.request("GET","/api/interviews/catalog",user=user)
        self.assertEqual(status,200);self.assertIn("java",catalog["languages"]);self.assertIn("cpp",catalog["languages"])
        status,session=self.create_interview()
        self.assertEqual(status,201);interview_id=session["id"]
        status,foreign=self.request("GET",f"/api/interviews/{interview_id}",user=other)
        self.assertEqual(status,404)
        self.assertNotIn("expected_output",json.dumps(session));self.assertNotIn("solution_code",json.dumps(session))
        code=feature11_12.QUESTIONS[0]["solutions"]["python"]
        status,draft=self.request("POST",f"/api/interviews/{interview_id}/questions/1/draft",{"code":code},user=user)
        self.assertEqual(status,200);self.assertTrue(draft["saved"])
        status,restored=self.request("GET",f"/api/interviews/{interview_id}",user=user)
        self.assertEqual(status,200);self.assertEqual(restored["questions"][0]["code"],code)
        status,run=self.request("POST",f"/api/interviews/{interview_id}/questions/1/run",{"code":code},user=user)
        self.assertEqual(status,200);self.assertTrue(run["result"]["all_passed"])
        self.assertNotIn("expected_output",json.dumps(run))
        with patch("tutor_engine.generate_ai",side_effect=RuntimeError("provider unavailable")):
            status,followup=self.request("POST",f"/api/interviews/{interview_id}/questions/1/ask",{"kind":"hint","code":code},user=user)
        self.assertEqual(status,200);self.assertEqual(followup["provider"],"local");self.assertNotIn("solution",followup["question"].lower())
        status,submitted=self.request("POST",f"/api/interviews/{interview_id}/questions/1/submit",{"code":code,"approach":"I scan each input value once and add it to a running total.","complexity":"O(n) time and O(n) input storage.","edge_cases":"A single negative value and an all-zero input."},user=user)
        self.assertEqual(status,200);self.assertEqual(submitted["question_score"],100);self.assertEqual(submitted["report"]["passed_tests"],4)
        report=submitted["report"]
        self.assertEqual(report["status"],"Completed");self.assertEqual(report["topic_performance"]["Arrays"]["accuracy"],100)
        self.assertIn("not subjectively scored",report["scoring_rule"])
        self.assertNotIn("expected_output",json.dumps(report))
        status,again=self.request("POST",f"/api/interviews/{interview_id}/questions/1/submit",{"code":code},user=user)
        self.assertEqual(status,409)
        status,report_response=self.request("GET",f"/api/interviews/{interview_id}/report",user=user)
        self.assertEqual(status,200);self.assertEqual(report_response["report"]["total_score"],100)
        status,history=self.request("GET","/api/interviews/history",user=user)
        self.assertEqual(status,200);self.assertEqual(history["interviews"][0]["status"],"completed")

    def test_interview_validation_timer_expiry_and_single_login(self):
        user=self.users[0]
        status,unauth=self.request("GET","/api/interviews/catalog")
        self.assertEqual(status,401)
        status,bad=self.create_interview(language="ruby")
        self.assertEqual(status,400)
        status,session=self.create_interview(duration_minutes=15)
        self.assertEqual(status,201)
        conn=database.get_connection();conn.execute("UPDATE interviews SET created_at=? WHERE id=?",(int(time.time())-901,session["id"]));conn.commit();conn.close()
        status,expired=self.request("GET",f"/api/interviews/{session['id']}",user=user)
        self.assertEqual(status,200);self.assertEqual(expired["status"],"expired");self.assertEqual(expired["report"]["status"],"Expired")
        status,run=self.request("POST",f"/api/interviews/{session['id']}/questions/1/run",{"code":"print(1)"},user=user)
        self.assertEqual(status,409)

    def test_multi_question_interview_finishes_each_question_in_order(self):
        user=self.users[0]
        status,session=self.create_interview(topic="Mixed",difficulty="Mixed",question_count=3)
        self.assertEqual(status,201);self.assertEqual(len(session["questions"]),3)
        for question in session["questions"]:
            item=next(q for q in feature11_12.QUESTIONS if q["id"]==question["id"])
            code=item["solutions"]["python"]
            status,result=self.request("POST",f"/api/interviews/{session['id']}/questions/{question['position']}/submit",
                {"code":code,"approach":"I trace the input and update a small state for each item.","complexity":"O(n) time","edge_cases":"I checked empty and smallest inputs."},user=user)
            self.assertEqual(status,200)
        self.assertEqual(result["report"]["questions_completed"],3)
        self.assertEqual(result["report"]["question_count"],3)
        self.assertEqual(result["report"]["total_score"],100)

    def test_competition_creation_hidden_data_scoring_duplicate_and_history(self):
        owner,second,third=self.users
        status,choices=self.request("GET","/api/competition-challenges/catalog?language=python&difficulty=Easy",user=owner)
        self.assertEqual(status,200);self.assertEqual(len(choices["challenges"]),1);self.assertNotIn("category",choices["challenges"][0])
        status,created=self.create_competition()
        self.assertEqual(status,201);comp_id=created["competition"]["id"]
        status,public=self.request("GET",f"/api/competitions/{comp_id}",user=owner)
        self.assertEqual(status,200);self.assertNotIn("buggy_code",json.dumps(public));self.assertNotIn("solution_code",json.dumps(public));self.assertNotIn("tests_json",json.dumps(public))
        self.assertEqual(self.request("POST",f"/api/competitions/{comp_id}/join",{},user=owner)[0],201)
        self.assertEqual(self.request("POST",f"/api/competitions/{comp_id}/join",{},user=owner)[0],409)
        self.assertEqual(self.request("POST",f"/api/competitions/{comp_id}/join",{},user=second)[0],201)
        self.assertEqual(self.request("POST",f"/api/competitions/{comp_id}/join",{},user=third)[0],409)
        start=created["competition"]["starts_at"]
        self.assertEqual(self.request("POST",f"/api/competitions/{comp_id}/start",{},user=owner)[0],409)
        with patch("server.time.time",return_value=start+1):
            status,challenge=self.request("POST",f"/api/competitions/{comp_id}/start",{},user=owner)
            self.assertEqual(status,200);self.assertIn("buggy_code",challenge)
            self.assertNotIn("solution_code",json.dumps(challenge));self.assertNotIn("expected_output",json.dumps(challenge));self.assertNotIn("tests_json",json.dumps(challenge))
            second_status,_=self.request("POST",f"/api/competitions/{comp_id}/start",{},user=second)
            self.assertEqual(second_status,200)
            status,unauthorized=self.request("POST",f"/api/competitions/{comp_id}/start",{},user=third)
            self.assertEqual(status,404)
            correct="n = int(input())\nprint('Even' if n % 2 == 0 else 'Odd')\n"
            wrong="print('not the requested result')\n"
            status,good=self.request("POST",f"/api/competitions/{comp_id}/submit",{"code":correct,"identified_category":"incorrect_conditions","explanation":"The condition checks parity backwards, so even input is labeled odd."},user=owner)
            self.assertEqual(status,200);self.assertEqual(good["submission"]["score"],100);self.assertEqual(good["submission"]["passed_tests"],2)
            status,bad=self.request("POST",f"/api/competitions/{comp_id}/submit",{"code":wrong,"identified_category":"syntax_errors","explanation":"The output does not match the requested even or odd label."},user=second)
            self.assertEqual(status,200);self.assertEqual(bad["submission"]["passed_tests"],0);self.assertEqual(bad["submission"]["score"],0)
            status,duplicate=self.request("POST",f"/api/competitions/{comp_id}/submit",{"code":correct,"identified_category":"incorrect_conditions","explanation":"Repeated submission should not score."},user=owner)
            self.assertEqual(status,409)
            status,board=self.request("GET",f"/api/competitions/{comp_id}/leaderboard",user=owner)
        self.assertEqual(status,200);self.assertEqual(board["leaderboard"][0]["display_name"],"Learner 1")
        self.assertNotIn("email",json.dumps(board));self.assertNotIn("code",json.dumps(board));self.assertNotIn("solution",json.dumps(board))
        status,history=self.request("GET","/api/competitions/history",user=owner)
        self.assertEqual(status,200);self.assertEqual(history["competitions"][0]["score"],100)
        status,foreign_history=self.request("GET","/api/competitions/history",user=third)
        self.assertEqual(status,200);self.assertEqual(foreign_history["competitions"],[])

    def test_owner_created_challenge_validation_catalog_privacy_and_competition_use(self):
        owner,other,_=self.users
        challenge={"title":"Parity labels","description":"Print Even for an even integer and Odd otherwise.",
            "language":"python","difficulty":"Easy","topic":"Problem Solving","bug_category":"incorrect_conditions",
            "buggy_code":"n=int(input())\nprint('Even' if n % 2 != 0 else 'Odd')\n",
            "solution_code":"n=int(input())\nprint('Even' if n % 2 == 0 else 'Odd')\n",
            "tests":[{"input":"8\n","expected_output":"Even\n"},{"input":"5\n","expected_output":"Odd\n"}]}
        status,created=self.request("POST","/api/competition-challenges",challenge,user=owner)
        self.assertEqual(status,201);custom_id=created["challenge"]["id"]
        status,catalog=self.request("GET","/api/competition-challenges/catalog?language=python&difficulty=Easy&all=1",user=owner)
        self.assertEqual(status,200);entry=next(x for x in catalog["challenges"] if x["id"]==custom_id)
        self.assertEqual(entry["source"],"Your custom challenge");self.assertNotIn("solution_code",json.dumps(entry));self.assertNotIn("buggy_code",json.dumps(entry))
        _,other_catalog=self.request("GET","/api/competition-challenges/catalog?language=python&difficulty=Easy&all=1",user=other)
        self.assertNotIn(custom_id,json.dumps(other_catalog))
        now=int(time.time())
        status,competition=self.create_competition(title="Custom challenge contest",topic="Problem Solving",challenge_id=custom_id,
            starts_at=now+30,ends_at=now+900)
        self.assertEqual(status,201);self.assertEqual(competition["competition"]["challenge_count"],1)
        status,foreign=self.request("POST","/api/competitions",{"title":"Foreign selection","description":"Should not expose another user's challenge.",
            "language":"python","difficulty":"Easy","topic":"Problem Solving","challenge_ids":[custom_id],
            "starts_at":now+30,"ends_at":now+900,"duration_minutes":5,"max_participants":2},user=other)
        self.assertEqual(status,400)
        invalid=dict(challenge);invalid["title"]="Invalid reference";invalid["solution_code"]="print('wrong')\n"
        self.assertEqual(self.request("POST","/api/competition-challenges",invalid,user=owner)[0],400)

    def test_problem_catalog_selection_creates_competition_without_missing_concept_index(self):
        owner=self.users[0]
        java_bug="public class Main { public static void main(String[] args) { java.util.Scanner s=new java.util.Scanner(System.in); int n=s.nextInt(); System.out.println(n+1); } }"
        java_solution="public class Main { public static void main(String[] args) { java.util.Scanner s=new java.util.Scanner(System.in); int n=s.nextInt(); System.out.println(n); } }"
        conn=database.get_connection()
        conn.execute("INSERT INTO problems(slug,title,language,difficulty,concept,mistake_category_slug,description,buggy_code,solution_code,test_cases_json,stages_config_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("custom-challenge-index-regression","Index regression","java","Easy","Problem Solving","algorithm_logic","Print the input integer.",java_bug,java_solution,json.dumps([{"input":"2\n","expected_output":"2\n"},{"input":"0\n","expected_output":"0\n"}]),"{}"))
        conn.commit();conn.close()
        status,catalog=self.request("GET","/api/competition-challenges/catalog?language=java&difficulty=Easy&all=1",user=owner)
        self.assertEqual(status,200)
        for entry in catalog["challenges"]:
            if not entry["id"].startswith("problem:"):continue
            problem=feature11_12._competition_challenge(entry["id"],"java","Easy")
            if not problem:continue
            reference=server.code_executor.execute_code(problem["solution_code"],"java",problem["tests"])
            buggy=server.code_executor.execute_code(problem["buggy_code"],"java",problem["tests"])
            bug_is_distinct=(not buggy["all_passed"] and (problem["bug_category"]=="syntax_errors" or buggy["is_compiled"]))
            if reference["is_compiled"] and reference["all_passed"] and bug_is_distinct:
                selected=entry;break
        else:self.skipTest("No validated Java Easy problem available in the local catalog")
        now=int(time.time())
        status,created=self.create_competition(title="Catalog problem selection",language="java",difficulty="Easy",
            topic=selected["topic"],challenge_id=selected["id"],starts_at=now+30,ends_at=now+900)
        self.assertEqual(status,201,created)

    @unittest.skipUnless(shutil.which("javac") and shutil.which("java"),"Java compiler/runtime not installed")
    def test_owner_created_java_if_else_challenge_can_create_competition(self):
        owner=self.users[0]
        challenge={"title":"Number sign classification","description":"Print Positive, Negative, or Zero for one integer.",
            "language":"java","difficulty":"Easy","topic":"Problem Solving","bug_category":"incorrect_conditions",
            "buggy_code":"import java.util.Scanner;\npublic class Main { public static void main(String[] args) { Scanner s=new Scanner(System.in); int n=s.nextInt(); if(n>0) System.out.println(\"Positive\"); else if(n>0) System.out.println(\"Negative\"); else System.out.println(\"Zero\"); } }\n",
            "solution_code":"import java.util.Scanner;\npublic class Main { public static void main(String[] args) { Scanner s=new Scanner(System.in); int n=s.nextInt(); if(n>0) System.out.println(\"Positive\"); else if(n<0) System.out.println(\"Negative\"); else System.out.println(\"Zero\"); } }\n",
            "tests":[{"input":"5\n","expected_output":"Positive\n"},{"input":"-3\n","expected_output":"Negative\n"},{"input":"0\n","expected_output":"Zero\n"}]}
        status,saved=self.request("POST","/api/competition-challenges",challenge,user=owner)
        self.assertEqual(status,201,saved)
        status,catalog=self.request("GET","/api/competition-challenges/catalog?language=java&difficulty=Easy&all=1",user=owner)
        self.assertEqual(status,200)
        self.assertIn(saved["challenge"]["id"],[entry["id"] for entry in catalog["challenges"]])
        now=int(time.time())
        status,competition=self.create_competition(title="Java condition challenge",language="java",topic="Problem Solving",
            challenge_id=saved["challenge"]["id"],starts_at=now+30,ends_at=now+900)
        self.assertEqual(status,201,competition)

    def test_competition_submission_is_atomic_and_end_time_is_enforced(self):
        owner,second,_=self.users
        status,created=self.create_competition(max_participants=3)
        self.assertEqual(status,201);comp_id=created["competition"]["id"];start=created["competition"]["starts_at"]
        self.assertEqual(self.request("POST",f"/api/competitions/{comp_id}/join",{},user=owner)[0],201)
        with patch("server.time.time",return_value=start+1):
            self.assertEqual(self.request("POST",f"/api/competitions/{comp_id}/start",{},user=owner)[0],200)
        code="n = int(input())\nprint('Even' if n % 2 == 0 else 'Odd')\n"
        outcomes=[];lock=threading.Lock()
        def submit_once():
            result=self.request("POST",f"/api/competitions/{comp_id}/submit",{"code":code,"identified_category":"incorrect_conditions","explanation":"Parity condition determines the correct even or odd output."},user=owner)
            with lock:outcomes.append(result[0])
        with patch("server.time.time",return_value=start+2):
            threads=[threading.Thread(target=submit_once) for _ in range(2)]
            for t in threads:t.start()
            for t in threads:t.join(timeout=20)
        self.assertEqual(sorted(outcomes),[200,409])
        status,other_created=self.create_competition(title="End-window test",starts_at=int(time.time())+20,ends_at=int(time.time())+25,duration_minutes=5)
        self.assertEqual(status,201);other_id=other_created["competition"]["id"]
        self.assertEqual(self.request("POST",f"/api/competitions/{other_id}/join",{},user=second)[0],201)
        status,denied=self.request("POST",f"/api/competitions/{other_id}/submit",{"code":"print(1)","identified_category":"incorrect_conditions","explanation":"This submission is outside the active time window."},user=second)
        self.assertEqual(status,409)

    def test_competition_room_code_lobby_host_start_and_synchronized_timer(self):
        host,participant,outsider=self.users
        status,created=self.create_competition(max_participants=3)
        self.assertEqual(status,201);comp_id=created["competition"]["id"]
        status,details=self.request("GET",f"/api/competitions/{comp_id}",user=host)
        self.assertEqual(status,200);room_code=details["room_code"]
        self.assertGreaterEqual(len(room_code),6)
        status,preview=self.request("GET",f"/api/competition-rooms/{room_code}",user=participant)
        self.assertEqual(status,200);self.assertTrue(preview["can_join"]);self.assertNotIn("solution_code",json.dumps(preview))
        status,uninvited=self.request("GET",f"/api/competitions/{comp_id}",user=outsider)
        self.assertEqual(status,200);self.assertNotIn("room_code",uninvited)
        self.assertEqual(self.request("POST","/api/competitions/join-by-code",{"room_code":room_code},user=participant)[0],201)
        self.assertEqual(self.request("POST",f"/api/competitions/{comp_id}/join",{},user=host)[0],201)
        status,lobby=self.request("GET",f"/api/competitions/{comp_id}/lobby",user=host)
        self.assertEqual(status,200);self.assertEqual(len(lobby["participants"]),2)
        self.assertEqual(self.request("POST",f"/api/competitions/{comp_id}/ready",{"ready":True},user=participant)[0],200)
        self.assertEqual(self.request("GET",f"/api/competitions/{comp_id}/lobby",user=outsider)[0],404)
        starts=created["competition"]["starts_at"]
        with patch("server.time.time",return_value=starts+1):
            self.assertEqual(self.request("POST",f"/api/competitions/{comp_id}/host-start",{},user=participant)[0],403)
            status,started=self.request("POST",f"/api/competitions/{comp_id}/host-start",{},user=host)
            self.assertEqual(status,200)
            lobby=self.request("GET",f"/api/competitions/{comp_id}/lobby",user=participant)[1]
            self.assertEqual(lobby["started_at"],started["started_at"])
            self.assertTrue(next(x for x in lobby["participants"] if x["user_id"]==participant)["ready"])
            _,first=self.request("POST",f"/api/competitions/{comp_id}/start",{},user=host)
            _,second=self.request("POST",f"/api/competitions/{comp_id}/start",{},user=participant)
            self.assertEqual(first["remaining_seconds"],second["remaining_seconds"])
            self.assertEqual(first["buggy_code"],second["buggy_code"])
        self.assertEqual(self.request("POST",f"/api/competitions/{comp_id}/join",{},user=outsider)[0],409)

    def test_multiround_competition_sample_submission_aggregate_and_history(self):
        owner,second,_=self.users;now=int(time.time())
        cases={
            "round-one":{"title":"First output check","description":"Print one for the given sample.","language":"python","difficulty":"Easy","bug_category":"incorrect_conditions","buggy_code":"print(0)\n","solution_code":"print(1)\n","tests":[{"input":"","expected_output":"1"}]},
            "round-two":{"title":"Second output check","description":"Print two for the given sample.","language":"python","difficulty":"Easy","bug_category":"incorrect_conditions","buggy_code":"print(0)\n# second task\n","solution_code":"print(2)\n","tests":[{"input":"","expected_output":"2"}]}}
        with patch("server._competition_challenge",side_effect=lambda selection,language,difficulty:cases.get(selection)):
            status,created=self.create_competition(challenge_ids=list(cases),max_participants=3,scoring_rule="tests_only")
        self.assertEqual(status,201);comp_id=created["competition"]["id"];start=created["competition"]["starts_at"]
        self.assertEqual(created["competition"]["challenge_count"],2)
        self.assertEqual(self.request("POST",f"/api/competitions/{comp_id}/join",{},user=owner)[0],201)
        self.assertEqual(self.request("POST",f"/api/competitions/{comp_id}/join",{},user=second)[0],201)
        with patch("server.time.time",return_value=start+1):
            self.assertEqual(self.request("POST",f"/api/competitions/{comp_id}/host-start",{},user=owner)[0],200)
            status,round_one=self.request("GET",f"/api/competitions/{comp_id}/rounds/1",user=owner)
            self.assertEqual(status,200);self.assertEqual(round_one["title"],cases["round-one"]["title"])
            status,sample=self.request("POST",f"/api/competitions/{comp_id}/rounds/1/run",{"code":"print(1)"},user=owner)
            self.assertEqual(status,200);self.assertTrue(sample["passed"])
            status,first=self.request("POST",f"/api/competitions/{comp_id}/rounds/1/submit",{"code":"print(1)","identified_category":"syntax_errors","explanation":"The original condition produced the wrong output value."},user=owner)
            self.assertEqual(status,200);self.assertEqual(first["submission"]["score"],100);self.assertFalse(first["submission"]["identification_correct"]);self.assertFalse(first["competition_complete"])
            status,progress=self.request("GET",f"/api/competitions/{comp_id}/status",user=owner)
            self.assertEqual(status,200);self.assertEqual(progress["participation"]["next_round"],2)
            self.assertEqual(self.request("GET",f"/api/competitions/{comp_id}/rounds/2",user=second)[0],200)
            status,last=self.request("POST",f"/api/competitions/{comp_id}/rounds/2/submit",{"code":"print(2)","identified_category":"incorrect_conditions","explanation":"The printed value must match the requested result."},user=owner)
            self.assertEqual(status,200);self.assertTrue(last["competition_complete"])
        status,history=self.request("GET","/api/competitions/history",user=owner)
        self.assertEqual(status,200);self.assertEqual(history["competitions"][0]["score"],100)
        owner_status=self.request("GET",f"/api/competitions/{comp_id}/status",user=owner)[1]["participation"]
        self.assertEqual(owner_status["rounds"][0]["code"],"print(1)")
        other_status=self.request("GET",f"/api/competitions/{comp_id}/status",user=second)[1]["participation"]
        self.assertIsNone(other_status["rounds"][0]["code"])
        board=self.request("GET",f"/api/competitions/{comp_id}/leaderboard",user=second)[1]
        self.assertEqual(board["leaderboard"][0]["challenges_solved"],2)

    def test_competition_leave_is_persisted_and_releases_capacity(self):
        host,participant,third=self.users
        status,created=self.create_competition(max_participants=2)
        self.assertEqual(status,201);comp_id=created["competition"]["id"]
        self.assertEqual(self.request("POST",f"/api/competitions/{comp_id}/join",{},user=host)[0],201)
        self.assertEqual(self.request("POST",f"/api/competitions/{comp_id}/join",{},user=participant)[0],201)
        self.assertEqual(self.request("POST",f"/api/competitions/{comp_id}/leave",{},user=participant)[0],200)
        status,participant_status=self.request("GET",f"/api/competitions/{comp_id}/status",user=participant)
        self.assertEqual(status,200);self.assertEqual(participant_status["participation"]["status"],"left")
        status,lobby=self.request("GET",f"/api/competitions/{comp_id}/lobby",user=host)
        self.assertEqual(status,200);self.assertEqual(next(p for p in lobby["participants"] if p["user_id"]==participant)["status"],"left")
        self.assertEqual(self.request("POST",f"/api/competitions/{comp_id}/join",{},user=participant)[0],409)
        self.assertEqual(self.request("POST",f"/api/competitions/{comp_id}/join",{},user=third)[0],201)
        self.assertEqual(self.request("GET",f"/api/competitions/{comp_id}",user=host)[1]["participants"],2)

    @unittest.skipUnless(shutil.which("javac") and shutil.which("java"),"Java compiler/runtime not installed")
    def test_java_interview_code_runs_through_existing_executor(self):
        java="import java.util.*;\npublic class Main { public static void main(String[] args) { Scanner s=new Scanner(System.in); int n=s.nextInt(); long total=0; for(int i=0;i<n;i++) total+=s.nextInt(); System.out.println(total); } }\n"
        result=server.code_executor.execute_code(java,"java",[{"input":"3\n2 3 4\n","expected_output":"9"}])
        self.assertTrue(result["is_compiled"]);self.assertTrue(result["all_passed"])

    def test_main_page_and_every_curated_competition_template_are_integrated_and_validated(self):
        status,page=self.request("GET","/")
        self.assertEqual(status,200)
        self.assertIn('data-view="interviews"',page);self.assertIn('data-view="competitions"',page)
        self.assertIn('src="features11_12.js"',page);self.assertNotIn('features11_12_server.py',page)
        for language in feature11_12.LANGUAGES:
            generator_language="c" if language=="cpp" else language
            for difficulty in feature11_12.DIFFICULTIES:
                challenge=server._local_bug_challenge(generator_language,difficulty)
                self.assertIsNotNone(challenge,(language,difficulty))
                buggy=server._format_feature11_12_challenge(challenge["buggy_code"],language)
                reference=server._format_feature11_12_challenge(challenge["solution_code"],language)
                good=server.code_executor.execute_code(reference,language,challenge["test_cases"])
                bad=server.code_executor.execute_code(buggy,language,challenge["test_cases"])
                self.assertTrue(good.get("is_compiled") and good.get("all_passed"),(language,difficulty,"reference",good))
                self.assertTrue(bad.get("is_compiled") and not bad.get("all_passed"),(language,difficulty,"bug",bad))

    def test_windows_c_fallback_recognizes_literal_even_odd_condition(self):
        challenge=server._local_bug_challenge("c","Easy")
        code=server._format_feature11_12_challenge(challenge["solution_code"],"c")
        outcomes=[server.code_executor._run_c_fallback(code,case) for case in challenge["test_cases"]]
        self.assertEqual([x["actual"].strip() for x in outcomes],["Even","Odd"])
        self.assertTrue(all(x["passed"] for x in outcomes))

    def test_windows_c_fallback_calculates_bounded_recursive_factorial(self):
        challenge=server._local_bug_challenge("c","Hard")
        code=server._format_feature11_12_challenge(challenge["solution_code"],"c")
        outcomes=[server.code_executor._run_c_fallback(code,case) for case in challenge["test_cases"]]
        self.assertEqual([x["actual"].strip() for x in outcomes],["1","120"])
        self.assertTrue(all(x["passed"] for x in outcomes))


if __name__=="__main__":
    unittest.main(verbosity=2)
