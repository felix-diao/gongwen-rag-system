--
-- PostgreSQL database dump
--

-- Dumped from database version 17.2 (Debian 17.2-1.pgdg120+1)
-- Dumped by pg_dump version 17.2 (Debian 17.2-1.pgdg120+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: knowledge_bases; Type: TABLE; Schema: public; Owner: gongwen_user
--

CREATE TABLE public.knowledge_bases (
    id integer NOT NULL,
    name character varying(100) NOT NULL,
    key character varying(50),
    description text,
    user_id character varying(64) NOT NULL,
    item_count integer,
    total_size integer,
    created_at timestamp without time zone,
    updated_at timestamp without time zone
);


ALTER TABLE public.knowledge_bases OWNER TO gongwen_user;

--
-- Name: knowledge_bases_id_seq; Type: SEQUENCE; Schema: public; Owner: gongwen_user
--

CREATE SEQUENCE public.knowledge_bases_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.knowledge_bases_id_seq OWNER TO gongwen_user;

--
-- Name: knowledge_bases_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: gongwen_user
--

ALTER SEQUENCE public.knowledge_bases_id_seq OWNED BY public.knowledge_bases.id;


--
-- Name: knowledge_bases id; Type: DEFAULT; Schema: public; Owner: gongwen_user
--

ALTER TABLE ONLY public.knowledge_bases ALTER COLUMN id SET DEFAULT nextval('public.knowledge_bases_id_seq'::regclass);


--
-- Data for Name: knowledge_bases; Type: TABLE DATA; Schema: public; Owner: gongwen_user
--

COPY public.knowledge_bases (id, name, key, description, user_id, item_count, total_size, created_at, updated_at) FROM stdin;
1	blog	\N	\N	user_0ce122b4e3274757	0	0	2025-11-24 12:32:45.352888	2025-11-24 12:44:42.060859
3	紧急通知	紧急	紧急情况使用	user_2275cf0346284274	0	0	2025-11-26 08:23:48.023162	2025-11-26 08:23:48.023165
4	日常通知	日常	\N	user_2275cf0346284274	4	14344383	2025-11-26 08:24:48.194464	2025-11-26 08:34:01.499481
2	放到	\N	\N	user_0ce122b4e3274757	0	0	2025-11-24 12:44:33.530526	2025-11-28 14:39:07.958295
5	公文模板知识库	\N	\N	user_8736a11aad4549dc	3	34846	2025-12-02 13:56:50.006733	2025-12-02 14:07:06.616745
6	生成语料库	\N	\N	user_8736a11aad4549dc	3	34846	2025-12-02 14:07:41.147821	2025-12-02 14:10:33.701091
7	课程改期通知	\N	\N	user_8736a11aad4549dc	0	0	2025-12-02 15:28:28.296759	2025-12-02 15:28:28.296763
\.


--
-- Name: knowledge_bases_id_seq; Type: SEQUENCE SET; Schema: public; Owner: gongwen_user
--

SELECT pg_catalog.setval('public.knowledge_bases_id_seq', 7, true);


--
-- Name: knowledge_bases knowledge_bases_pkey; Type: CONSTRAINT; Schema: public; Owner: gongwen_user
--

ALTER TABLE ONLY public.knowledge_bases
    ADD CONSTRAINT knowledge_bases_pkey PRIMARY KEY (id);


--
-- Name: ix_knowledge_bases_key; Type: INDEX; Schema: public; Owner: gongwen_user
--

CREATE INDEX ix_knowledge_bases_key ON public.knowledge_bases USING btree (key);


--
-- Name: ix_knowledge_bases_name; Type: INDEX; Schema: public; Owner: gongwen_user
--

CREATE INDEX ix_knowledge_bases_name ON public.knowledge_bases USING btree (name);


--
-- Name: ix_knowledge_bases_user_id; Type: INDEX; Schema: public; Owner: gongwen_user
--

CREATE INDEX ix_knowledge_bases_user_id ON public.knowledge_bases USING btree (user_id);


--
-- Name: knowledge_bases knowledge_bases_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: gongwen_user
--

ALTER TABLE ONLY public.knowledge_bases
    ADD CONSTRAINT knowledge_bases_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(user_id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

